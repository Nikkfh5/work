"""
supervisor/stages/deliver.py — Stage: CI + push + TG notify.

Финальный stage: выполняет CI/push для repos и уведомляет через TG.

Инварианты:
- Запускается только если worker_status == "done" или "reviewed_approved"
- CI/push через safe_exec (allowlist runner)
- release_lease done после успешного push
- TG уведомление всегда
"""

import logging
from typing import Callable, Optional

from supervisor.lease_manager import release_lease
from supervisor.pipeline import (
    E_GIT_PUSH_FAIL,
    WorkerContext,
    _fail_final,
)
from supervisor.safe_exec import safe_exec

logger = logging.getLogger(__name__)


async def _run_ci_and_push(
    task_id: str,
    job: str,
    alias: str,
    worker_cfg: dict,
    repo_mgr,
    db_path: Optional[str] = None,
    executor: Optional[Callable] = None,
) -> tuple[bool, str]:
    """
    Запустить style formatters, git commit, CI checks, git push.

    Returns:
        (success, error_message)
    """
    exec_fn = executor or safe_exec
    wt_path = str(repo_mgr._worktree_path(task_id, alias))

    # 1. Style policy — run formatters before commit
    style_policy = worker_cfg.get("style_policy", {})
    if style_policy.get("run_before_commit"):
        for fmt_cmd in style_policy.get("formatters", []):
            try:
                _out, _err, rc = exec_fn(fmt_cmd, cwd=wt_path, timeout=120)
                if rc != 0:
                    logger.warning(
                        "_run_ci_and_push: formatter %s failed task_id=%s rc=%d",
                        fmt_cmd[0],
                        task_id,
                        rc,
                    )
            except Exception as exc:
                logger.warning(
                    "_run_ci_and_push: formatter error task_id=%s: %s",
                    task_id,
                    exc,
                )

    # 2. Git add + commit
    try:
        add_out, add_err, add_rc = exec_fn(["git", "add", "."], cwd=wt_path, timeout=60)
        if add_rc != 0:
            logger.warning("_run_ci_and_push: git add failed task_id=%s rc=%d stderr=%s", task_id, add_rc, add_err[:200])
        _out, _err, rc = exec_fn(
            ["git", "commit", "-m", f"ai: task {task_id[:8]} — auto-commit"],
            cwd=wt_path,
            timeout=60,
        )
        if rc != 0:
            logger.warning("_run_ci_and_push: git commit rc=%d task_id=%s stdout=%s stderr=%s", rc, task_id, _out[:200], _err[:200])
            # Nothing to commit is OK (rc=1 with "nothing to commit")
            if "nothing to commit" not in _out and "nothing to commit" not in _err:
                return False, f"git commit failed (rc={rc}): {_err[:200]}"
    except Exception as exc:
        return False, f"git commit error: {str(exc)[:200]}"

    # 3. CI policy — run checks before push
    ci_policy = worker_cfg.get("ci_policy", {})
    ci_required = ci_policy.get("required_pass", False)
    for ci_cmd in ci_policy.get("run_before_push", []):
        try:
            _out, _err, rc = exec_fn(ci_cmd, cwd=wt_path, timeout=300)
            if rc != 0 and ci_required:
                return False, f"CI failed ({ci_cmd[0]}): {_err[:200]}"
        except Exception as exc:
            if ci_required:
                return (
                    False,
                    f"CI error ({ci_cmd[0] if ci_cmd else '?'}): {str(exc)[:200]}",
                )

    # 4. Git push
    try:
        _out, _err, rc = exec_fn(
            ["git", "push", "origin", "HEAD"],
            cwd=wt_path,
            timeout=120,
        )
        if rc != 0:
            return False, f"git push failed (rc={rc}): {_err[:200]}"
    except Exception as exc:
        return False, f"git push error: {str(exc)[:200]}"

    logger.info("_run_ci_and_push: success task_id=%s", task_id)
    return True, ""


def _build_ci_fix_prompt(
    task_description: str,
    ci_error: str,
    attempt: int,
    max_attempts: int,
    repos_context: Optional[list] = None,
    branch: str = "",
) -> str:
    """Build prompt for worker to fix CI failures."""
    workspace_section = ""
    if repos_context:
        lines = [f"Ветка: {branch}", "Репозитории:"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        workspace_section = "\n".join(lines) + "\n\n"

    return (
        f"CI/push проверка провалилась. Исправь ошибки.\n"
        f"\n"
        f"Задача: {task_description[:200]}\n"
        f"\n"
        f"Ошибка CI:\n"
        f"{ci_error[:1000]}\n"
        f"\n"
        f"{workspace_section}"
        f"Инструкция:\n"
        f"1. Прочитай ошибку и пойми что сломалось\n"
        f"2. Исправь ТОЛЬКО то, что вызвало ошибку CI\n"
        f"3. НЕ трогай другой код\n"
        f"4. Это попытка {attempt}/{max_attempts} автоисправления\n"
        f"\n"
        f"После исправления выведи:\n"
        f"<<<JSON>>>\n"
        f'{{\n'
        f'  "status": "done",\n'
        f'  "confidence": 80,\n'
        f'  "result": {{\n'
        f'    "repos": [],\n'
        f'    "notes": "CI fix: <что исправлено>"\n'
        f'  }},\n'
        f'  "question": null\n'
        f'}}\n'
        f"<<<END>>>\n"
    )


async def deliver_stage(ctx: WorkerContext) -> None:
    """
    Stage: CI + push + release_lease done + TG notify.

    Запускается только для done / reviewed_approved.
    Для blocked / error — уже обработано в review_stage.
    """
    if ctx.parsed is None:
        return

    # Если review/execute stage уже обработали ошибку — skip
    if ctx.worker_status in ("_blocked_handled", "_error_handled", "_review_handled", "_session_refresh"):
        return

    # Explorer / report mode: just send result to TG, no CI/push
    delivery_mode = ctx.worker_cfg.get("delivery_policy", {}).get("mode", "push")
    if delivery_mode == "report":
        notes = ctx.parsed.get("result", {}).get("notes", "")
        release_lease(ctx.task_id, ctx.worker_id, ctx.token, "done", db_path=ctx.db_path)
        ctx.emit("task_done", confidence=ctx.confidence, notes=f"[Explorer report]\n{notes}", cost_usd=ctx.cumulative_cost_usd)
        logger.info("deliver_stage: report mode done task_id=%s", ctx.task_id)
        return

    # CI + push for repos (with auto-fix loop)
    if ctx.repos_context:
        max_ci_fix_attempts = int(ctx.worker_cfg.get("ci_auto_fix_attempts", 3))
        repos = ctx.worker_cfg.get("repos", [])

        for repo in repos:
            success = False
            err = ""

            for ci_attempt in range(1, max_ci_fix_attempts + 1):
                success, err = await _run_ci_and_push(
                    ctx.task_id,
                    ctx.job,
                    repo["alias"],
                    ctx.worker_cfg,
                    ctx.repo_manager,
                    ctx.db_path,
                    executor=ctx.executor,
                )
                if success:
                    break

                if ci_attempt == max_ci_fix_attempts:
                    break  # exhausted

                # Auto-fix: send CI error to worker for fixing
                logger.info(
                    "deliver_stage: CI failed, auto-fix attempt %d/%d task_id=%s",
                    ci_attempt,
                    max_ci_fix_attempts,
                    ctx.task_id,
                )

                fix_prompt = _build_ci_fix_prompt(
                    ctx.task_description,
                    err,
                    ci_attempt,
                    max_ci_fix_attempts,
                    repos_context=ctx.repos_context,
                    branch=ctx.branch,
                )

                try:
                    from supervisor.claude_runner import run_claude as _default_runner
                    from supervisor.cost_tracker import run_claude_tracked

                    runner = ctx.runner or _default_runner
                    # Use worktree path so fixes land in the right place
                    fix_cwd = str(ctx.repo_manager._worktree_path(ctx.task_id, repo["alias"])) if ctx.repo_manager else ctx.worker_dir
                    _fix_stdout, _fix_metrics = await run_claude_tracked(
                        fix_prompt, cwd=fix_cwd, timeout=ctx.worker_timeout, runner=runner
                    )
                    ctx.cumulative_cost_usd += _fix_metrics.get("cost_usd", 0)
                    ctx.cumulative_input_tokens += _fix_metrics.get("input_tokens", 0)
                    ctx.cumulative_output_tokens += _fix_metrics.get("output_tokens", 0)
                    ctx.cumulative_elapsed_ms += _fix_metrics.get("elapsed_ms", 0)

                    # Emit notification about auto-fix attempt
                    ctx.emit("ci_auto_fix", attempt=ci_attempt, error=err[:200])
                except Exception as exc:
                    logger.warning(
                        "deliver_stage: auto-fix runner failed task_id=%s: %s",
                        ctx.task_id,
                        exc,
                    )
                    break  # Can't fix, fall through to failure

            if not success:
                _fail_final(
                    ctx.task_id,
                    ctx.worker_id,
                    ctx.token,
                    E_GIT_PUSH_FAIL,
                    ctx.db_path,
                )
                ctx.emit(
                    "task_failed",
                    reason="git_push_failed",
                    message=f"CI/push failed after {max_ci_fix_attempts} auto-fix attempts: {err}",
                )
                return

    # Release lease done
    release_lease(ctx.task_id, ctx.worker_id, ctx.token, "done", db_path=ctx.db_path)

    # Emit completion event
    if ctx.worker_status == "reviewed_approved":
        iteration = ctx.parsed.get("_review_iteration", "?")
        feedback = ctx.parsed.get("_review_feedback", "")
        ctx.emit(
            "review_approved",
            message=f"DONE (reviewer APPROVED, iter={iteration})\n{feedback[:200]}",
        )
        ctx.emit("task_done", confidence=ctx.confidence, notes="", cost_usd=ctx.cumulative_cost_usd)
    else:
        notes = ctx.parsed.get("result", {}).get("notes", "")
        ctx.emit("task_done", confidence=ctx.confidence, notes=notes, cost_usd=ctx.cumulative_cost_usd)

    logger.info("deliver_stage: done task_id=%s", ctx.task_id)
