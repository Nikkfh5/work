"""
tests/test_pipeline.py — тесты для supervisor/pipeline.py (pipeline engine).

Запуск: pytest tests/test_pipeline.py -v

Тестируем:
- Pipeline прогоняет все stages по порядку
- Pipeline останавливается на StageError → 3rd не вызван + _fail_final
- LeaseConflict → тихий выход (без _fail_final, без notify)
- Cleanup worktrees всегда в finally
- WorkerContext создаётся с правильными полями
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from supervisor.pipeline import (
    WorkerContext,
    StageError,
    LeaseConflict,
    WorkerCrash,
    run_pipeline,
    _worker_to_job,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_ctx(**overrides) -> WorkerContext:
    """Создать WorkerContext с дефолтами для тестов."""
    defaults = dict(
        task_id="test-task-123",
        worker_id="job1_worker",
        task_description="test task",
        config={"supervisor": {}, "workers": {"job1_worker": {}}},
        tg_handler=AsyncMock(),
        db_path=None,
        worker_cfg={},
        job="job1",
        worker_dir="workers/job1_worker",
    )
    defaults.update(overrides)
    return WorkerContext(**defaults)


# ── test_pipeline_runs_all_stages ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_runs_all_stages():
    """3 stages, все вызваны по порядку."""
    ctx = _make_ctx()
    call_order = []

    async def stage_a(c):
        call_order.append("a")

    async def stage_b(c):
        call_order.append("b")

    async def stage_c(c):
        call_order.append("c")

    await run_pipeline(ctx, [stage_a, stage_b, stage_c])
    assert call_order == ["a", "b", "c"]


# ── test_pipeline_stops_on_stage_error ───────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_stops_on_stage_error(db_path):
    """2nd stage throws StageError → 3rd не вызван + _fail_final."""
    ctx = _make_ctx(db_path=db_path, token="tok-123")
    call_order = []

    async def stage_a(c):
        call_order.append("a")

    async def stage_b(c):
        raise WorkerCrash(reason="worker_crash", message="boom")

    async def stage_c(c):
        call_order.append("c")

    # Need a task in DB for _fail_final
    from storage.db import create_task

    create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="test",
        client_contact="42",
    )

    await run_pipeline(ctx, [stage_a, stage_b, stage_c])

    assert call_order == ["a"]  # c not called
    ctx.tg_handler.notify_owner.assert_called_once()
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "boom" in msg


# ── test_pipeline_lease_conflict_silent ──────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_lease_conflict_silent():
    """LeaseConflict → тихий выход (не notify, не _fail_final)."""
    ctx = _make_ctx()

    async def stage_conflict(c):
        raise LeaseConflict()

    async def stage_never(c):
        raise AssertionError("should not be called")

    await run_pipeline(ctx, [stage_conflict, stage_never])

    # notify_owner НЕ вызван
    ctx.tg_handler.notify_owner.assert_not_called()


# ── test_pipeline_cleanup_always_runs ────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_cleanup_always_runs():
    """error → finally cleanup worktrees."""
    mock_mgr = MagicMock()
    mock_mgr.cleanup_worktree = MagicMock()

    ctx = _make_ctx(
        worktree_aliases=["api", "frontend"],
        repo_manager=mock_mgr,
        job="job1",
    )

    async def stage_boom(c):
        raise RuntimeError("unexpected")

    await run_pipeline(ctx, [stage_boom])

    assert mock_mgr.cleanup_worktree.call_count == 2
    cleanup_aliases = [c[0][2] for c in mock_mgr.cleanup_worktree.call_args_list]
    assert "api" in cleanup_aliases
    assert "frontend" in cleanup_aliases


# ── test_worker_context_creation ─────────────────────────────────────────────


def test_worker_context_creation():
    """ctx создаётся с правильными полями."""
    ctx = WorkerContext(
        task_id="abc-123",
        worker_id="job1_worker",
        task_description="do something",
        config={"supervisor": {}},
        tg_handler=AsyncMock(),
        db_path="/tmp/test.db",
        worker_cfg={"max_attempts": 5},
        job="job1",
        worker_dir="workers/job1_worker",
        repos=[{"alias": "api", "url": "https://github.com/org/api"}],
        max_attempts=5,
        conf_threshold=80,
    )

    assert ctx.task_id == "abc-123"
    assert ctx.worker_id == "job1_worker"
    assert ctx.job == "job1"
    assert ctx.max_attempts == 5
    assert ctx.conf_threshold == 80
    assert ctx.repos == [{"alias": "api", "url": "https://github.com/org/api"}]
    assert ctx.token == ""
    assert ctx.parsed is None
    assert ctx.worktree_aliases == []


# ── test_worker_to_job ───────────────────────────────────────────────────────


def test_worker_to_job():
    """_worker_to_job extracts job name correctly."""
    assert _worker_to_job("job1_worker") == "job1"
    assert _worker_to_job("frontend_worker") == "frontend"
    assert _worker_to_job("solo") == "solo"


# ── test_pipeline_cleanup_on_success ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_cleanup_on_success():
    """Cleanup runs even on successful pipeline completion."""
    mock_mgr = MagicMock()
    mock_mgr.cleanup_worktree = MagicMock()

    ctx = _make_ctx(
        worktree_aliases=["api"],
        repo_manager=mock_mgr,
        job="job1",
    )

    async def stage_ok(c):
        pass

    await run_pipeline(ctx, [stage_ok])
    mock_mgr.cleanup_worktree.assert_called_once_with("test-task-123", "job1", "api")


# ── test_pipeline_no_cleanup_without_worktrees ───────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_no_cleanup_without_worktrees():
    """No worktrees → no cleanup calls."""
    mock_mgr = MagicMock()
    ctx = _make_ctx(repo_manager=mock_mgr)
    assert ctx.worktree_aliases == []

    async def stage_ok(c):
        pass

    await run_pipeline(ctx, [stage_ok])
    mock_mgr.cleanup_worktree.assert_not_called()


# ── test_stage_error_with_notify_false ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stage_error_with_notify_false(db_path):
    """StageError with notify=False → no TG notification."""
    ctx = _make_ctx(db_path=db_path, token="tok-123")

    from storage.db import create_task

    create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="test",
        client_contact="42",
    )

    async def stage_silent_fail(c):
        raise StageError(reason="test_reason", message="silent", notify=False)

    await run_pipeline(ctx, [stage_silent_fail])
    ctx.tg_handler.notify_owner.assert_not_called()
