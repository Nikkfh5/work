"""
supervisor/stages/review.py — Stage: reviewer cycle.

Обработка результата воркера:
- done + high confidence + reviewer → review cycle
- done + high confidence, no reviewer → skip (deliver stage)
- blocked / low confidence → escalation
- error → WorkerCrash

Review cycle:
  reviewer проверяет → APPROVED → готово
  NEEDS_CHANGES → worker retry → reviewer → повтор
  exhausted iterations → requires_manual

Инварианты:
- При APPROVED — ctx.worker_status остаётся "done" для deliver stage
- При отсутствии reviewer — просто passthrough
- Escalation (blocked) — release_lease blocked
"""

import asyncio
import json
import logging
from typing import Optional

from storage.db import get_conn
from supervisor.cost_tracker import run_claude_tracked
from supervisor.lease_manager import release_lease
from supervisor.pipeline import (
    E_JSON_SCHEMA,
    E_REVIEW_EXHAUSTED,
    E_REVIEWER_CRASH,
    E_WORKER_CRASH,
    WorkerContext,
    _fail_final,
)

logger = logging.getLogger(__name__)


def _build_reviewer_prompt(
    task_description: str,
    worker_result: dict,
    repos_context: Optional[list[dict]] = None,
    iteration: int = 1,
    prev_issues: Optional[list[dict]] = None,
) -> str:
    """Промпт для ревьюера: задание + результат воркера + JSON формат."""
    notes = worker_result.get("notes", "")
    repos_info = worker_result.get("repos", [])
    changed_files_section = ""
    for repo in repos_info:
        alias = repo.get("alias", "?")
        files = repo.get("changed_files", [])
        if files:
            changed_files_section += f"\n  {alias}: {', '.join(str(f) for f in files)}"

    workspace_section = ""
    if repos_context:
        lines = ["Репозитории:"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        workspace_section = "\n".join(lines) + "\n\n"

    # На повторных итерациях — фокус на проверке исправлений, не на поиске новых проблем
    re_review_section = ""
    if iteration > 1 and prev_issues:
        issues_lines = []
        for iss in prev_issues:
            file = iss.get("file", "?")
            msg = iss.get("message", "")
            issues_lines.append(f"  - {file}: {msg}")
        re_review_section = f"""
ВНИМАНИЕ: Это повторное ревью (итерация {iteration}).
На прошлой итерации ты нашёл эти замечания:
{chr(10).join(issues_lines)}

Воркер сообщил что исправил: {notes}

Твоя задача — ТОЛЬКО проверить что предыдущие замечания исправлены.
НЕ ищи новых проблем. НЕ повышай планку. Если предыдущие issues пофикшены — APPROVED.
"""

    return f"""\
Ты — ревьюер. Проверь результат воркера по заданию.

─────────────────────────────────────────────
Задание:
{task_description}

─────────────────────────────────────────────
Результат воркера:
{notes}

Изменённые файлы:{changed_files_section if changed_files_section else " (нет)"}

{workspace_section}{re_review_section}─────────────────────────────────────────────
Проверь:
1. Код соответствует заданию
2. Нет явных ошибок или уязвимостей
3. Тесты покрывают основные сценарии

Будь прагматичным: APPROVED если код работает и выполняет задание.
Мелкие стилевые замечания — не повод для NEEDS_CHANGES.
NEEDS_CHANGES только при реальных багах, сломанной логике или отсутствии тестов.

Выведи результат СТРОГО в этом формате:

<<<JSON>>>
{{
  "verdict": "APPROVED" или "NEEDS_CHANGES",
  "feedback": "<общий комментарий>",
  "issues": [
    {{"repo": "<alias>", "file": "<путь>", "line": null, "type": "bug|style|logic|test", "message": "<описание>"}}
  ]
}}
<<<END>>>

Если всё ОК — verdict "APPROVED" и пустой issues.
Если есть замечания — verdict "NEEDS_CHANGES" и заполни issues.
"""


def _build_worker_retry_prompt(
    task_description: str,
    attempt: int,
    max_attempts: int,
    prev_notes: str,
    reviewer_issues: list[dict],
    repos_context: Optional[list[dict]] = None,
    branch: str = "",
) -> str:
    """Промпт для повторной попытки воркера после NEEDS_CHANGES."""
    issues_lines = []
    for iss in reviewer_issues:
        repo = iss.get("repo", "?")
        file = iss.get("file", "?")
        line = iss.get("line")
        iss_type = iss.get("type", "?")
        msg = iss.get("message", "")
        loc = f"{file}:{line}" if line else file
        issues_lines.append(f"  - {repo}/{loc} [{iss_type}] {msg}")

    issues_text = (
        "\n".join(issues_lines) if issues_lines else "  (нет конкретных замечаний)"
    )

    workspace_section = ""
    if repos_context:
        lines = [f"Ветка: {branch}", "Репозитории:"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        lines.append("Работай ТОЛЬКО в этих директориях.")
        workspace_section = "\n".join(lines) + "\n\n"

    return f"""\
Задача: {task_description}
Попытка: {attempt}/{max_attempts}

Предыдущий результат: {prev_notes}

Замечания ревьюера (ИСПРАВЬ ИМЕННО ЭТО):
{issues_text}

Инструкция:
1. Прочитай файлы которые ты менял на предыдущей попытке
2. Исправь ТОЛЬКО указанные замечания — точечно, минимальным diff
3. НЕ переписывай с нуля. НЕ трогай то, что уже работает
4. Если замечание содержит конкретный fix (regex, код) — используй его как подсказку
5. Это попытка {attempt} из {max_attempts} — если не исправишь, задача уйдёт на ручное разбирательство

─────────────────────────────────────────────
{workspace_section}ОБЯЗАТЕЛЬНО: после выполнения выведи результат СТРОГО в этом формате:

<<<JSON>>>
{{
  "status": "done",
  "confidence": <0-100>,
  "result": {{
    "repos": [{", ".join(f'{{"alias": "{r["alias"]}", "changed_files": [...], "entrypoint": null}}' for r in (repos_context or []))}],
    "notes": "<что именно исправлено>"
  }},
  "question": null
}}
<<<END>>>
"""


async def _run_review_cycle(ctx: WorkerContext) -> None:
    """
    Цикл review: reviewer проверяет -> при NEEDS_CHANGES -> worker retry -> повтор.

    При APPROVED -> ctx.worker_status остаётся "done" для deliver stage.
    При exhausted iterations -> release_lease requires_manual + TG.
    """
    from supervisor.claude_runner import ClaudeRunnerError
    from supervisor.claude_runner import run_claude as _default_runner
    from supervisor.json_guard import extract_json, validate_reviewer_schema
    from supervisor.json_guard import validate_worker_schema
    from supervisor.run_logger import log_run

    runner = ctx.runner or _default_runner

    worker_cfg = ctx.worker_cfg
    reviewer_id = worker_cfg.get("reviewer_id")
    reviewer_dir = f"workers/{reviewer_id}"
    max_iterations = int(worker_cfg.get("max_review_iterations", 3))
    current_worker_result = ctx.parsed.get("result", {})
    prev_issues: list[dict] = []  # issues from previous iteration for re-review focus

    # BUG-005 fix: создаём symlink для reviewer чтобы он видел worktree файлы
    if ctx.repo_manager and ctx.worktree_aliases:
        ctx.repo_manager.ensure_agent_symlink(ctx.task_id, reviewer_id)

    for iteration in range(1, max_iterations + 1):
        logger.info(
            "review_stage: iteration %d/%d task_id=%s",
            iteration,
            max_iterations,
            ctx.task_id,
        )

        # Build reviewer prompt (on re-review: focus on prev issues, not new ones)
        reviewer_prompt = _build_reviewer_prompt(
            ctx.task_description,
            current_worker_result,
            ctx.repos_context,
            iteration=iteration,
            prev_issues=prev_issues if iteration > 1 else None,
        )

        # Run reviewer — always use reviewer model (stricter review with Opus)
        reviewer_model = ctx.config.get("supervisor", {}).get("model_routing", {}).get(
            "reviewer", ctx.model
        )
        try:
            reviewer_stdout, reviewer_metrics = await run_claude_tracked(
                reviewer_prompt, cwd=reviewer_dir, timeout=ctx.worker_timeout,
                runner=runner, model=reviewer_model or None,
            )
            ctx.cumulative_cost_usd += reviewer_metrics.get("cost_usd", 0)
            ctx.cumulative_input_tokens += reviewer_metrics.get("input_tokens", 0)
            ctx.cumulative_output_tokens += reviewer_metrics.get("output_tokens", 0)
            ctx.cumulative_elapsed_ms += reviewer_metrics.get("elapsed_ms", 0)
        except (ClaudeRunnerError, asyncio.TimeoutError) as exc:
            logger.error(
                "review_stage: reviewer error task_id=%s: %s", ctx.task_id, exc
            )
            _fail_final(
                ctx.task_id, ctx.worker_id, ctx.token, E_REVIEWER_CRASH, ctx.db_path
            )
            ctx.emit(
                "task_failed",
                reason="reviewer_crash",
                message=f"reviewer crashed: {str(exc)[:150]}",
            )
            ctx.worker_status = "_review_handled"
            return

        # Parse reviewer response
        reviewer_parsed = extract_json(reviewer_stdout)
        reviewer_valid, reviewer_err = (
            validate_reviewer_schema(reviewer_parsed)
            if reviewer_parsed
            else (False, "no JSON from reviewer")
        )

        log_run(
            task_id=ctx.task_id,
            phase="reviewer",
            stdout=reviewer_stdout,
            stderr="",
            parsed_json=reviewer_parsed,
            json_valid=reviewer_valid,
            worker_id=reviewer_id,
            db_path=ctx.db_path,
            returncode=0,
            metrics=reviewer_metrics,
        )

        if not reviewer_valid:
            logger.error(
                "review_stage: invalid reviewer json task_id=%s err=%s",
                ctx.task_id,
                reviewer_err,
            )
            _fail_final(
                ctx.task_id, ctx.worker_id, ctx.token, E_JSON_SCHEMA, ctx.db_path
            )
            ctx.emit(
                "task_failed",
                reason="json_schema_invalid",
                message=f"reviewer JSON invalid: {reviewer_err[:150]}",
            )
            ctx.worker_status = "_review_handled"
            return

        verdict = reviewer_parsed.get("verdict")

        if verdict == "APPROVED":
            # Review passed — deliver stage will handle CI/push
            feedback = reviewer_parsed.get("feedback", "")
            ctx.worker_status = "reviewed_approved"
            # Store iteration info for deliver stage notification
            ctx.parsed["_review_iteration"] = iteration
            ctx.parsed["_review_feedback"] = feedback
            logger.info(
                "review_stage: approved task_id=%s iteration=%d",
                ctx.task_id,
                iteration,
            )
            return

        # NEEDS_CHANGES — retry worker
        is_last = iteration == max_iterations
        if is_last:
            # Save accumulated progress for potential retry
            _save_partial_progress(ctx, current_worker_result, reviewer_parsed)

            # Check if persistent completion is enabled
            persistent_enabled = ctx.worker_cfg.get("persistent_completion", False)
            max_total_attempts = int(ctx.worker_cfg.get("max_total_review_cycles", 2))

            # Read current review cycle count from DB
            current_cycle = _get_review_cycle(ctx.task_id, ctx.db_path)

            if persistent_enabled and current_cycle < max_total_attempts:
                # Auto-retry: release lease as blocked, will be re-dispatched
                _increment_review_cycle(ctx.task_id, ctx.db_path)
                release_lease(
                    ctx.task_id, ctx.worker_id, ctx.token, "blocked",
                    db_path=ctx.db_path,
                )
                ctx.emit(
                    "review_needs_changes",
                    iteration=iteration,
                    feedback=f"Auto-retry cycle {current_cycle + 1}/{max_total_attempts}. "
                             f"Previous feedback: {reviewer_parsed.get('feedback', '')[:200]}",
                )
                logger.info(
                    "review_stage: persistent retry task_id=%s cycle=%d/%d",
                    ctx.task_id, current_cycle + 1, max_total_attempts,
                )
                ctx.worker_status = "_review_handled"
                return

            # Truly exhausted — requires_manual
            _fail_final(
                ctx.task_id,
                ctx.worker_id,
                ctx.token,
                E_REVIEW_EXHAUSTED,
                ctx.db_path,
            )
            feedback = reviewer_parsed.get("feedback", "")
            ctx.emit(
                "task_failed",
                reason="review_exhausted",
                message=(
                    f"review exhausted ({max_iterations} iterations x {current_cycle + 1} cycles).\n{feedback[:200]}"
                ),
            )
            logger.warning(
                "review_stage: exhausted task_id=%s iterations=%d cycles=%d",
                ctx.task_id, max_iterations, current_cycle + 1,
            )
            ctx.worker_status = "_review_handled"
            return

        # Save issues for next reviewer iteration (re-review focus)
        prev_issues = reviewer_parsed.get("issues", [])

        # Build worker retry prompt and re-run worker
        prev_notes = current_worker_result.get("notes", "")
        issues = prev_issues
        retry_prompt = _build_worker_retry_prompt(
            ctx.task_description,
            attempt=iteration + 1,
            max_attempts=max_iterations,
            prev_notes=prev_notes,
            reviewer_issues=issues,
            repos_context=ctx.repos_context,
            branch=ctx.branch,
        )

        try:
            worker_stdout, worker_metrics = await run_claude_tracked(
                retry_prompt, cwd=ctx.worker_dir, timeout=ctx.worker_timeout,
                runner=runner, model=ctx.model or None,
            )
            ctx.cumulative_cost_usd += worker_metrics.get("cost_usd", 0)
            ctx.cumulative_input_tokens += worker_metrics.get("input_tokens", 0)
            ctx.cumulative_output_tokens += worker_metrics.get("output_tokens", 0)
            ctx.cumulative_elapsed_ms += worker_metrics.get("elapsed_ms", 0)
        except (ClaudeRunnerError, asyncio.TimeoutError) as exc:
            logger.error(
                "review_stage: worker retry error task_id=%s: %s",
                ctx.task_id,
                exc,
            )
            _fail_final(
                ctx.task_id, ctx.worker_id, ctx.token, E_WORKER_CRASH, ctx.db_path
            )
            ctx.emit(
                "task_failed",
                reason="worker_crash",
                message=f"worker retry crashed: {str(exc)[:150]}",
            )
            ctx.worker_status = "_review_handled"
            return

        # Parse worker retry result
        worker_parsed = extract_json(worker_stdout)
        worker_valid, worker_err = (
            validate_worker_schema(worker_parsed)
            if worker_parsed
            else (False, "no JSON from worker retry")
        )

        log_run(
            task_id=ctx.task_id,
            phase="worker",
            stdout=worker_stdout,
            stderr="",
            parsed_json=worker_parsed,
            json_valid=worker_valid,
            worker_id=ctx.worker_id,
            db_path=ctx.db_path,
            returncode=0,
            metrics=worker_metrics,
        )

        if not worker_valid:
            logger.error(
                "review_stage: worker retry invalid json task_id=%s err=%s",
                ctx.task_id,
                worker_err,
            )
            _fail_final(
                ctx.task_id, ctx.worker_id, ctx.token, E_JSON_SCHEMA, ctx.db_path
            )
            ctx.emit(
                "task_failed",
                reason="json_schema_invalid",
                message=f"worker retry JSON invalid: {worker_err[:150]}",
            )
            ctx.worker_status = "_review_handled"
            return

        # Update current result for next review iteration
        current_worker_result = worker_parsed.get("result", {})


def _save_partial_progress(ctx: WorkerContext, worker_result: dict, reviewer_parsed: dict) -> None:
    """Save accumulated progress from review cycle in partial_result for potential retry."""
    progress = {
        "last_worker_notes": worker_result.get("notes", ""),
        "last_reviewer_feedback": reviewer_parsed.get("feedback", ""),
        "last_reviewer_issues": reviewer_parsed.get("issues", []),
    }

    try:
        with get_conn(ctx.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET partial_result=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps(progress, ensure_ascii=False), ctx.task_id),
            )
    except Exception as exc:
        logger.warning("_save_partial_progress failed task_id=%s: %s", ctx.task_id, exc)


def _get_review_cycle(task_id: str, db_path=None) -> int:
    """Get current review cycle count from review_iteration field in DB."""
    try:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT review_iteration FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        return row["review_iteration"] if row else 0
    except Exception:
        return 0


def _increment_review_cycle(task_id: str, db_path=None) -> None:
    """Increment review_iteration counter in DB."""
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE tasks SET review_iteration=review_iteration+1, "
                "updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )
    except Exception as exc:
        logger.warning("_increment_review_cycle failed task_id=%s: %s", task_id, exc)


async def review_stage(ctx: WorkerContext) -> None:
    """
    Stage: обработка результата воркера + review cycle.

    Логика:
    - parsed is None → execute stage не завершился успешно, skip
    - done + high confidence + reviewer → review cycle
    - done + high confidence, no reviewer → passthrough to deliver
    - blocked / low confidence → escalation
    - error → fail
    """
    if ctx.parsed is None:
        # execute stage завершился с fail (already handled), skip
        return

    worker_status = ctx.worker_status
    confidence = ctx.confidence

    if worker_status == "done" and confidence >= ctx.conf_threshold:
        # Check if reviewer is configured
        reviewer_id = ctx.worker_cfg.get("reviewer_id")
        if reviewer_id:
            await _run_review_cycle(ctx)
            return
        # No reviewer — passthrough to deliver stage
        return

    if worker_status == "blocked" or (
        worker_status == "done" and confidence < ctx.conf_threshold
    ):
        from supervisor.escalation import handle_worker_blocked

        task_dict = {
            "id": ctx.task_id,
            "assigned_worker": ctx.worker_id,
            "description": ctx.task_description,
        }
        result = await handle_worker_blocked(
            task_dict, ctx.parsed, ctx.config, ctx.tg_handler, ctx.db_path
        )
        if result.startswith("directive:"):
            logger.info(
                "review_stage: auto-resolved task_id=%s directive=%s",
                ctx.task_id,
                result[10:50],
            )
        release_lease(
            ctx.task_id, ctx.worker_id, ctx.token, "blocked", db_path=ctx.db_path
        )
        logger.warning("review_stage: blocked task_id=%s", ctx.task_id)
        # Mark as handled — deliver stage should not run
        ctx.worker_status = "_blocked_handled"
        return

    # "error" от воркера
    _fail_final(ctx.task_id, ctx.worker_id, ctx.token, E_WORKER_CRASH, ctx.db_path)
    ctx.emit("task_failed", reason="worker_crash", message="воркер вернул error.")
    logger.error(
        "review_stage: worker_error task_id=%s status=%s",
        ctx.task_id,
        worker_status,
    )
    ctx.worker_status = "_error_handled"
