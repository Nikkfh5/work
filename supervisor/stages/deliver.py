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
        exec_fn(["git", "add", "."], cwd=wt_path, timeout=60)
        _out, _err, rc = exec_fn(
            ["git", "commit", "-m", f"ai: task {task_id[:8]} — auto-commit"],
            cwd=wt_path,
            timeout=60,
        )
        if rc != 0:
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


async def deliver_stage(ctx: WorkerContext) -> None:
    """
    Stage: CI + push + release_lease done + TG notify.

    Запускается только для done / reviewed_approved.
    Для blocked / error — уже обработано в review_stage.
    """
    if ctx.parsed is None:
        return

    # Если review/execute stage уже обработали ошибку — skip
    if ctx.worker_status in ("_blocked_handled", "_error_handled", "_review_handled"):
        return

    # CI + push for repos
    if ctx.repos_context:
        repos = ctx.worker_cfg.get("repos", [])
        for repo in repos:
            success, err = await _run_ci_and_push(
                ctx.task_id,
                ctx.job,
                repo["alias"],
                ctx.worker_cfg,
                ctx.repo_manager,
                ctx.db_path,
                executor=ctx.executor,
            )
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
                    message=f"CI/push failed: {err}",
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
    else:
        notes = ctx.parsed.get("result", {}).get("notes", "")
        ctx.emit("task_done", confidence=ctx.confidence, notes=notes)

    logger.info("deliver_stage: done task_id=%s", ctx.task_id)
