"""
supervisor/stages/execute.py — Stage: run claude CLI + retry + JSON parse.

Retry loop воркера: до max_attempts попыток.
- ClaudeRunnerError (crash): повтор с тем же промптом после retry_delay
- json_invalid / json_schema_invalid: повтор с коррекционным промптом
- asyncio.TimeoutError: не ретраить (таймаут повторится)

Мутирует ctx: parsed, worker_status, confidence.

Инварианты:
- Промпт строится из описания задачи + workspace context
- JSON парсится через json_guard (extract_json + validate_worker_schema)
- Каждая попытка логируется через run_logger (log_run)
"""

import asyncio
import logging
from typing import Optional

from supervisor.cost_tracker import run_claude_tracked
from supervisor.pipeline import (
    E_JSON_INVALID,
    E_JSON_SCHEMA,
    E_SAFEEXEC_TIMEOUT,
    E_WORKER_CRASH,
    WorkerContext,
    _fail_final,
)
from supervisor.session_manager import (
    build_resumed_prompt,
    extract_progress_summary,
    load_checkpoint,
    mark_resumed,
    save_checkpoint,
    should_refresh_session,
)

logger = logging.getLogger(__name__)


def _build_worker_prompt(
    description: str,
    task_id: str = "",
    repos_context: Optional[list[dict]] = None,
    branch: str = "",
    plan_text: str = "",
) -> str:
    """
    Собрать полный промпт для воркера: задание + workspace context + JSON-вывод.

    repos_context: [{"alias": "api", "path": "workspace/{task_id}/api"}]
    plan_text: утверждённый план в JSON-формате (из planning_stage)
    """
    # Контекст рабочей директории
    workspace_section = ""
    if repos_context:
        lines = [f"Task ID: {task_id}", f"Ветка: {branch}", "Репозитории:"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        lines.append("Работай ТОЛЬКО в этих директориях. Не выходи за их пределы.")
        workspace_section = "\n".join(lines) + "\n\n"

    # Утверждённый план
    plan_section = ""
    if plan_text:
        plan_section = (
            "--- УТВЕРЖДЁННЫЙ ПЛАН ---\n"
            f"{plan_text}\n"
            "--- КОНЕЦ ПЛАНА ---\n\n"
            "Следуй утверждённому плану. Выполняй шаги последовательно.\n\n"
        )

    return f"""\
Задание от супервайзора:

{description}

{plan_section}

─────────────────────────────────────────────
{workspace_section}ОБЯЗАТЕЛЬНО: после выполнения задания выведи результат СТРОГО в этом формате
(без лишнего текста после <<<END>>>):

<<<JSON>>>
{{
  "status": "done",
  "confidence": <целое число 0-100>,
  "result": {{
    "repos": [{", ".join(f'{{"alias": "{r["alias"]}", "changed_files": [...], "entrypoint": null}}' for r in (repos_context or []))}],
    "notes": "<что именно сделано, одна-две строки>"
  }},
  "question": null
}}
<<<END>>>

Если задание непонятно или нужно уточнение — используй status "blocked" и заполни "question".
Если произошла ошибка — используй status "error" и опиши её в "notes".
confidence — твоя уверенность в правильности результата (0–100).
─────────────────────────────────────────────
"""


def _build_json_correction_prompt(previous_output: str) -> str:
    """
    Коррекционный промпт: показать что вышло и попросить JSON.

    Используется на повторных попытках когда воркер не вывел JSON-маркеры.
    """
    truncated = previous_output[:800] if len(previous_output) > 800 else previous_output
    return f"""\
В предыдущем ответе ты не вывел JSON в обязательном формате.

Твой предыдущий ответ:
{truncated}

Выведи результат ТОЛЬКО в этом формате (без лишнего текста):

<<<JSON>>>
{{
  "status": "done",
  "confidence": <0-100>,
  "result": {{
    "repos": [],
    "notes": "<что ты сделал>"
  }},
  "question": null
}}
<<<END>>>

Если задание не выполнено — используй status "blocked" (с "question") или "error".
"""


def _build_explorer_prompt(
    description: str,
    task_id: str = "",
    repos_context: Optional[list[dict]] = None,
) -> str:
    """
    Промпт для explorer (read-only агента): только анализ, без изменений файлов.

    Explorer не пишет код, а возвращает отчёт/ответ.
    """
    workspace_section = ""
    if repos_context:
        lines = [f"Task ID: {task_id}", "Репозитории (read-only):"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        lines.append("Ты можешь ТОЛЬКО ЧИТАТЬ файлы. НЕ изменяй ничего.")
        workspace_section = "\n".join(lines) + "\n\n"

    return f"""\
Ты — explorer-агент (read-only). Твоя задача — ИССЛЕДОВАТЬ и ОТВЕТИТЬ, \
не меняя код.

Задание:
{description}

─────────────────────────────────────────────
{workspace_section}Исследуй репозиторий и дай подробный ответ на вопрос/задание.

ОБЯЗАТЕЛЬНО: выведи результат СТРОГО в этом формате:

<<<JSON>>>
{{
  "status": "done",
  "confidence": <0-100>,
  "result": {{
    "repos": [],
    "notes": "<подробный ответ/отчёт>"
  }},
  "question": null
}}
<<<END>>>

Если нужно уточнение — используй status "blocked" и заполни "question".
"""


async def execute_stage(ctx: WorkerContext) -> None:
    """
    Retry loop: до max_attempts попыток запуска claude CLI.

    При успехе заполняет ctx.parsed, ctx.worker_status, ctx.confidence.
    При провале — raise WorkerCrash / StageError.
    """
    from supervisor.claude_runner import ClaudeRunnerError
    from supervisor.claude_runner import run_claude as _default_runner
    from supervisor.json_guard import extract_json, validate_worker_schema
    from supervisor.run_logger import log_run

    runner = ctx.runner or _default_runner

    last_stdout = ""
    use_correction = False
    is_explorer = ctx.worker_cfg.get("is_explorer", False)

    # Check for existing checkpoint (session resumed)
    checkpoint = load_checkpoint(ctx.task_id, ctx.db_path)
    if checkpoint:
        logger.info("execute_stage: resuming from checkpoint task_id=%s", ctx.task_id)
        mark_resumed(checkpoint["id"], ctx.db_path)

    for attempt in range(1, ctx.max_attempts + 1):
        is_last = attempt == ctx.max_attempts

        # Escalation: on last attempt, switch to stronger model
        if is_last and attempt > 1:
            esc_model = (
                ctx.config.get("supervisor", {})
                .get("model_routing", {})
                .get("escalation", "")
            )
            if esc_model and esc_model != ctx.model:
                logger.info(
                    "execute_stage: escalating model %s → %s (last attempt) task_id=%s",
                    ctx.model or "default",
                    esc_model,
                    ctx.task_id,
                )
                ctx.model = esc_model

        logger.info(
            "execute_stage: attempt %d/%d task_id=%s model=%s",
            attempt,
            ctx.max_attempts,
            ctx.task_id,
            ctx.model or "default",
        )

        # Промпт: json_invalid -> коррекционный, иначе полный
        if use_correction and last_stdout:
            prompt = _build_json_correction_prompt(last_stdout)
        elif checkpoint and attempt == 1:
            prompt = build_resumed_prompt(
                ctx.task_description,
                checkpoint,
                plan_text=ctx.plan_text,
            )
        elif is_explorer:
            prompt = _build_explorer_prompt(
                ctx.task_description,
                task_id=ctx.task_id,
                repos_context=ctx.repos_context,
            )
        else:
            prompt = _build_worker_prompt(
                ctx.task_description,
                task_id=ctx.task_id,
                repos_context=ctx.repos_context,
                branch=ctx.branch,
                plan_text=ctx.plan_text,
            )
        use_correction = False

        # Запустить claude CLI
        try:
            stdout, metrics = await run_claude_tracked(
                prompt,
                cwd=ctx.worker_dir,
                timeout=ctx.worker_timeout,
                runner=runner,
                model=ctx.model or None,
            )
            # Accumulate cost metrics
            ctx.cumulative_cost_usd += metrics.get("cost_usd", 0)
            ctx.cumulative_input_tokens += metrics.get("input_tokens", 0)
            ctx.cumulative_output_tokens += metrics.get("output_tokens", 0)
            ctx.cumulative_elapsed_ms += metrics.get("elapsed_ms", 0)
            last_stdout = stdout
        except asyncio.TimeoutError:
            logger.error(
                "execute_stage: timeout attempt=%d task_id=%s",
                attempt,
                ctx.task_id,
            )
            # Timeout — не ретраить, сразу fail
            _fail_final(
                ctx.task_id,
                ctx.worker_id,
                ctx.token,
                E_SAFEEXEC_TIMEOUT,
                ctx.db_path,
            )
            ctx.emit(
                "task_failed",
                reason="safeexec_timeout",
                message="\u0442\u0430\u0439\u043c\u0430\u0443\u0442 \u0432\u043e\u0440\u043a\u0435\u0440\u0430.",
            )
            return
        except ClaudeRunnerError as exc:
            logger.error(
                "execute_stage: claude error attempt=%d task_id=%s: %s",
                attempt,
                ctx.task_id,
                exc,
            )
            if is_last:
                _fail_final(
                    ctx.task_id,
                    ctx.worker_id,
                    ctx.token,
                    E_WORKER_CRASH,
                    ctx.db_path,
                )
                ctx.emit(
                    "task_failed",
                    reason="worker_crash",
                    message=(
                        f"\u0432\u043e\u0440\u043a\u0435\u0440 \u0443\u043f\u0430\u043b "
                        f"{ctx.max_attempts}\u00d7 \u043f\u043e\u0434\u0440\u044f\u0434.\n"
                        f"{str(exc)[:150]}"
                    ),
                )
                return
            logger.warning(
                "execute_stage: crash attempt=%d, retry in %ds task_id=%s",
                attempt,
                ctx.retry_delay,
                ctx.task_id,
            )
            await asyncio.sleep(ctx.retry_delay)
            continue

        # Check if session needs refresh
        if should_refresh_session(metrics):
            progress = extract_progress_summary(stdout)
            save_checkpoint(
                ctx.task_id,
                ctx.worker_id,
                "worker",
                attempt,
                metrics,
                progress,
                db_path=ctx.db_path,
            )
            from supervisor.lease_manager import release_lease

            release_lease(
                ctx.task_id, ctx.worker_id, ctx.token, "blocked", db_path=ctx.db_path
            )
            ctx.emit(
                "session_refresh",
                tokens=metrics.get("input_tokens", 0),
                cost=metrics.get("cost_usd", 0),
            )
            ctx.worker_status = "_session_refresh"
            logger.warning(
                "execute_stage: session refresh needed task_id=%s tokens=%d",
                ctx.task_id,
                metrics.get("input_tokens", 0),
            )
            return

        # Парсим и валидируем JSON
        parsed = extract_json(stdout)
        valid, err = validate_worker_schema(parsed) if parsed else (False, "no JSON")

        log_run(
            task_id=ctx.task_id,
            phase="worker",
            stdout=stdout,
            stderr="",
            parsed_json=parsed,
            json_valid=valid,
            worker_id=ctx.worker_id,
            db_path=ctx.db_path,
            returncode=0,  # runner raises on non-zero, so success = 0
            metrics=metrics,
        )

        if not valid:
            logger.warning(
                "execute_stage: invalid json attempt=%d/%d task_id=%s err=%s",
                attempt,
                ctx.max_attempts,
                ctx.task_id,
                err,
            )
            if is_last:
                reason = E_JSON_INVALID if parsed is None else E_JSON_SCHEMA
                _fail_final(
                    ctx.task_id,
                    ctx.worker_id,
                    ctx.token,
                    reason,
                    ctx.db_path,
                )
                ctx.emit(
                    "task_failed",
                    reason=reason,
                    message=(
                        f"\u0432\u043e\u0440\u043a\u0435\u0440 \u043d\u0435 \u0434\u0430\u043b "
                        f"JSON {ctx.max_attempts}\u00d7 ({err})."
                    ),
                )
                return
            use_correction = True
            logger.warning(
                "execute_stage: retrying with correction attempt=%d task_id=%s",
                attempt,
                ctx.task_id,
            )
            continue

        # JSON валидный — сохраняем результат в ctx
        ctx.parsed = parsed
        ctx.worker_status = parsed.get("status", "error")
        ctx.confidence = parsed.get("confidence", 0)
        return
