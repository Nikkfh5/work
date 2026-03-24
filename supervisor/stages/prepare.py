"""
supervisor/stages/prepare.py — Stage: lease acquisition + worktree setup.

Первый stage в pipeline: захватывает lease на задачу и подготавливает
worktrees для каждого repo из конфигурации воркера.

Инварианты:
- LeaseConflict → pipeline тихо завершается (не notify)
- WorktreeError → pipeline завершается с notify + requires_manual
- Мутирует ctx: token, worktree_aliases, repos_context, branch
"""

import logging
import os

from supervisor.lease_manager import acquire_lease
from supervisor.pipeline import LeaseConflict, WorkerContext, WorktreeError

logger = logging.getLogger(__name__)


def _setup_worktrees(
    task_id: str,
    job: str,
    repos: list[dict],
    branch_pattern: str,
    base_branch: str,
    repo_mgr,
) -> list[str]:
    """
    Подготовить worktrees для каждого repo.

    Returns: list[str] — worktree aliases, которые были успешно созданы.
    Raises: Exception при ошибке (caller обработает).
    """
    worktree_aliases: list[str] = []
    for repo in repos:
        alias = repo["alias"]
        url = repo["url"]
        token_env = repo.get("token_env", "")
        git_token = os.getenv(token_env) if token_env else None

        repo_mgr.ensure_mirror(
            job,
            alias,
            url,
            clone_strategy=repo.get("clone_strategy", "mirror"),
            token=git_token,
        )

        branch = branch_pattern.replace("{task_id}", task_id)
        repo_mgr.prepare_worktree(task_id, job, alias, branch, base_branch)
        worktree_aliases.append(alias)

    logger.info(
        "prepare_stage: worktrees ready task_id=%s repos=%s",
        task_id,
        [r["alias"] for r in repos],
    )
    return worktree_aliases


async def prepare_stage(ctx: WorkerContext) -> None:
    """
    Захватить lease + подготовить worktrees.

    Raises:
        LeaseConflict: lease уже занят
        WorktreeError: ошибка при создании worktree
    """
    lease_ttl = int(os.getenv("WORKER_LEASE_TTL_SECONDS", "300"))
    token = acquire_lease(
        ctx.task_id, ctx.worker_id, ttl=lease_ttl, db_path=ctx.db_path
    )
    if not token:
        raise LeaseConflict()
    ctx.token = token

    if ctx.repos:
        try:
            ctx.worktree_aliases = _setup_worktrees(
                ctx.task_id,
                ctx.job,
                ctx.repos,
                ctx.branch_pattern,
                ctx.base_branch,
                ctx.repo_manager,
            )
            ctx.repos_context = [
                {
                    "alias": r["alias"],
                    "path": f"workspace/{ctx.task_id}/{r['alias']}",
                }
                for r in ctx.repos
            ]
            ctx.branch = ctx.branch_pattern.replace("{task_id}", ctx.task_id)
        except Exception as exc:
            logger.error(
                "prepare_stage: worktree setup failed task_id=%s: %s",
                ctx.task_id,
                exc,
            )
            raise WorktreeError(
                reason="worker_crash",
                message=f"не удалось подготовить worktree.\n{str(exc)[:150]}",
            ) from exc

    logger.info("prepare_stage: ready task_id=%s worker=%s", ctx.task_id, ctx.worker_id)
