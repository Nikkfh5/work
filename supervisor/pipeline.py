"""
supervisor/pipeline.py — Pipeline engine, WorkerContext dataclass, StageError hierarchy.

Центральный модуль pipeline-архитектуры: определяет контекст задачи,
иерархию ошибок stage'ей и движок прогона ctx через list[stage].

Инварианты:
- Pipeline НЕ знает про конкретные stages (только вызывает их)
- WorkerContext — единственный объект, передаваемый между stages
- StageError hierarchy управляет логикой обработки ошибок в run_pipeline
- Cleanup worktrees выполняется в finally (всегда)
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from storage.db import get_conn
from supervisor.lease_manager import release_lease

logger = logging.getLogger(__name__)

# Коды ошибок (see CLAUDE.md)
E_JSON_INVALID = "json_invalid"
E_JSON_SCHEMA = "json_schema_invalid"
E_SAFEEXEC_TIMEOUT = "safeexec_timeout"
E_WORKER_CRASH = "worker_crash"
E_GIT_PUSH_FAIL = "git_push_failed"
E_REVIEW_EXHAUSTED = "review_exhausted"
E_REVIEWER_CRASH = "reviewer_crash"


@dataclass
class WorkerContext:
    """Полный контекст задачи, передаваемый через все stages pipeline."""

    # Core — нужны всем stages
    task_id: str
    worker_id: str
    task_description: str
    config: dict
    tg_handler: object  # TelegramHandler or AsyncMock in tests

    db_path: Optional[str] = None

    # Worker config (derived)
    worker_cfg: dict = field(default_factory=dict)
    job: str = ""
    worker_dir: str = ""

    # Lease
    token: str = ""
    lease_ttl: int = 300

    # Worktree
    repos: list = field(default_factory=list)
    worktree_aliases: list = field(default_factory=list)
    branch: str = ""
    branch_pattern: str = "ai/task-{task_id}"
    base_branch: str = "main"
    repos_context: Optional[list] = None
    repo_manager: object = None  # RepoManager, injected

    # Execution
    max_attempts: int = 3
    worker_timeout: int = 1800
    retry_delay: int = 30
    conf_threshold: int = 70

    # Planning
    complexity: str = ""  # "simple" | "complex" | ""
    plan_text: str = ""  # structured plan JSON (stored in DB)
    plan_revision: int = 0  # current plan revision number
    planning_enabled: bool = False  # from config supervisor.planning.enabled
    planning_config: dict = field(default_factory=dict)  # supervisor.planning section

    # Model selection
    model: str = ""  # Claude model (e.g. "opus", "sonnet"). Empty → CLI default.

    # Cost tracking (accumulated across all runner calls in this pipeline)
    cumulative_cost_usd: float = 0.0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_elapsed_ms: int = 0

    # Result
    parsed: Optional[dict] = None
    worker_status: str = ""
    confidence: int = 0

    # DI: injectable dependencies (None -> use real implementations)
    runner: Optional[Callable] = None  # run_claude replacement
    executor: Optional[Callable] = None  # safe_exec replacement

    # Event bus
    _events: list = field(default_factory=list, repr=False)

    def emit(self, event_type: str, **kwargs) -> None:
        """Record an event. Processed by handle_events after each stage."""
        self._events.append({"type": event_type, "data": kwargs})

    def drain_events(self) -> list:
        """Return all accumulated events and clear the internal list."""
        events = self._events.copy()
        self._events.clear()
        return events


# ── StageError hierarchy ─────────────────────────────────────────────────────


class StageError(Exception):
    """Базовая ошибка stage."""

    def __init__(self, reason: str, message: str = "", notify: bool = True):
        super().__init__(message or reason)
        self.reason = reason
        self.notify = notify


class LeaseConflict(StageError):
    """Lease уже занят другим воркером — тихий выход."""

    def __init__(self):
        super().__init__("lease_conflict", notify=False)


class WorktreeError(StageError):
    """Ошибка при подготовке worktree."""

    pass


class WorkerCrash(StageError):
    """Воркер упал / timeout / не дал JSON."""

    pass


class ReviewExhausted(StageError):
    """Все итерации review исчерпаны."""

    pass


class CIFailed(StageError):
    """CI проверка или git push не прошли."""

    pass


class PlanRejected(StageError):
    """Владелец отклонил план задачи."""

    def __init__(self):
        super().__init__(reason="plan_rejected", message="Plan rejected by owner.")


class PlanningError(StageError):
    """Ошибка при создании плана (super-agent не смог создать план)."""

    pass


# ── Helper functions (moved from main.py) ────────────────────────────────────


def _set_error_reason(task_id: str, reason: str, db_path: Optional[str] = None) -> None:
    """Записать last_error_reason в tasks для задачи."""
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE tasks SET last_error_reason=? WHERE id=?",
                (reason, task_id),
            )
    except Exception as _e:
        logger.warning("_set_error_reason: could not set error reason: %s", _e)


def _fail_final(
    task_id: str,
    worker_id: str,
    token: str,
    reason: str,
    db_path: Optional[str] = None,
) -> None:
    """Финальный сбой после всех попыток -> requires_manual."""
    _set_error_reason(task_id, reason, db_path)
    release_lease(task_id, worker_id, token, "requires_manual", db_path=db_path)
    logger.error(
        "pipeline: requires_manual task_id=%s reason=%s",
        task_id,
        reason,
    )


async def _notify_failure(tg_handler, task_id: str, message: str) -> None:
    """Async helper: уведомить владельца об ошибке задачи (legacy, kept for compat)."""
    await tg_handler.notify_owner(
        f"\u26a0\ufe0f task#{task_id[:8]}: {message}\n\u041f\u043e\u0432\u0442\u043e\u0440\u0438: /retry {task_id[:8]}"
    )


def _worker_to_job(worker_id: str) -> str:
    """Извлечь имя job из worker_id: 'job1_worker' -> 'job1'."""
    return (
        worker_id.removesuffix("_worker")
        if worker_id.endswith("_worker")
        else worker_id
    )


# ── Pipeline engine ──────────────────────────────────────────────────────────


async def _background_lease_renewal(ctx: WorkerContext, stop_event) -> None:
    """
    Background task: renew lease every TTL/2 seconds while pipeline runs.

    Fixes BUG-014: without this, leases expire during long worker/review stages.
    Stops when stop_event is set (pipeline finished or errored).
    """
    import asyncio

    interval = max(ctx.lease_ttl // 2, 30)  # at least every 30s
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # stop_event was set
        except asyncio.TimeoutError:
            pass  # interval elapsed, time to renew

        if not ctx.token:
            return  # no lease yet (prepare_stage not run)

        renewed = renew_lease(
            ctx.task_id,
            ctx.worker_id,
            ctx.token,
            ttl=ctx.lease_ttl,
            db_path=ctx.db_path,
        )
        if not renewed:
            logger.warning(
                "background_lease_renewal: lease lost task_id=%s",
                ctx.task_id,
            )
            return  # lease gone, pipeline will fail naturally


async def run_pipeline(ctx: WorkerContext, stages: list) -> None:
    """
    Прогнать ctx через stages. Обработать ошибки, cleanup в finally.

    Launches a background lease renewal task that keeps the lease alive
    throughout the entire pipeline execution (BUG-014 fix).

    After each stage (and on errors), accumulated events are dispatched
    via handle_events from supervisor.event_handlers.

    Args:
        ctx: WorkerContext -- mutated by each stage
        stages: list[Callable[[WorkerContext], Awaitable[None]]]
    """
    import asyncio

    from supervisor.event_handlers import handle_events

    # Start background lease renewal
    stop_renewal = asyncio.Event()
    renewal_task = asyncio.create_task(
        _background_lease_renewal(ctx, stop_renewal),
        name=f"lease_renewal_{ctx.task_id[:8]}",
    )

    try:
        for stage in stages:
            await stage(ctx)
            await handle_events(ctx)
    except LeaseConflict:
        logger.warning("pipeline: lease conflict task_id=%s", ctx.task_id)
    except StageError as e:
        _fail_final(ctx.task_id, ctx.worker_id, ctx.token, e.reason, ctx.db_path)
        if e.notify:
            ctx.emit("task_failed", reason=e.reason, message=str(e))
            await handle_events(ctx)
    except Exception as exc:
        logger.error("pipeline: unexpected error task_id=%s: %s", ctx.task_id, exc)
        _set_error_reason(ctx.task_id, E_WORKER_CRASH, ctx.db_path)
        try:
            release_lease(
                ctx.task_id,
                ctx.worker_id,
                ctx.token,
                "requires_manual",
                db_path=ctx.db_path,
            )
        except Exception:
            pass
    finally:
        # Stop background lease renewal
        stop_renewal.set()
        renewal_task.cancel()
        try:
            await renewal_task
        except (asyncio.CancelledError, Exception):
            pass

        # Worktree cleanup
        if ctx.worktree_aliases and ctx.repo_manager:
            for alias in ctx.worktree_aliases:
                try:
                    ctx.repo_manager.cleanup_worktree(ctx.task_id, ctx.job, alias)
                except Exception as cleanup_exc:
                    logger.warning(
                        "pipeline: cleanup failed task_id=%s alias=%s: %s",
                        ctx.task_id,
                        alias,
                        cleanup_exc,
                    )
            # Cleanup reviewer symlink if reviewer was configured
            reviewer_id = ctx.worker_cfg.get("reviewer_id")
            if reviewer_id:
                try:
                    ctx.repo_manager.cleanup_agent_symlink(ctx.task_id, reviewer_id)
                except Exception as cleanup_exc:
                    logger.warning(
                        "pipeline: reviewer symlink cleanup failed task_id=%s: %s",
                        ctx.task_id,
                        cleanup_exc,
                    )
