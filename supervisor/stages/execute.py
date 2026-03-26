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

from supervisor.pipeline import (
    E_JSON_INVALID,
    E_JSON_SCHEMA,
    E_SAFEEXEC_TIMEOUT,
    E_WORKER_CRASH,
    WorkerContext,
    _fail_final,
)

logger = logging.getLogger(__name__)


def _build_worker_prompt(
    description: str,
    task_id: str = "",
    repos_context: Optional[list[dict]] = None,
    branch: str = "",
) -> str:
    """
    Собрать полный промпт для воркера: задание + workspace context + JSON-вывод.

    repos_context: [{"alias": "api", "path": "workspace/{task_id}/api"}]
    """
    # Контекст рабочей директории
    workspace_section = ""
    if repos_context:
        lines = [f"Task ID: {task_id}", f"Ветка: {branch}", "Репозитории:"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        lines.append("Работай ТОЛЬКО в этих директориях. Не выходи за их пределы.")
        workspace_section = "\n".join(lines) + "\n\n"

    return f"""\
Задание от супервайзора:

{description}

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

    for attempt in range(1, ctx.max_attempts + 1):
        is_last = attempt == ctx.max_attempts
        logger.info(
            "execute_stage: attempt %d/%d task_id=%s",
            attempt,
            ctx.max_attempts,
            ctx.task_id,
        )

        # Промпт: json_invalid -> коррекционный, иначе полный
        if use_correction and last_stdout:
            prompt = _build_json_correction_prompt(last_stdout)
        else:
            prompt = _build_worker_prompt(
                ctx.task_description,
                task_id=ctx.task_id,
                repos_context=ctx.repos_context,
                branch=ctx.branch,
            )
        use_correction = False

        # Запустить claude CLI
        try:
            stdout = await runner(
                prompt, cwd=ctx.worker_dir, timeout=ctx.worker_timeout
            )
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
