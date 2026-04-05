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
from unittest.mock import AsyncMock, MagicMock, patch

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

    with patch("supervisor.pipeline.is_lease_valid", return_value=True):
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

    with patch("supervisor.pipeline.is_lease_valid", return_value=True):
        await run_pipeline(ctx, [stage_silent_fail])
    ctx.tg_handler.notify_owner.assert_not_called()


# ── test_di_runner_used ────────────────────────────────────────────────────


WORKER_DONE_JSON = """\
<<<JSON>>>
{
  "status": "done",
  "confidence": 95,
  "result": {"repos": [], "notes": "DI test"},
  "question": null
}
<<<END>>>
"""


@pytest.mark.asyncio
async def test_di_runner_used(db_path):
    """ctx.runner is set → execute_stage uses it instead of real run_claude."""
    from supervisor.stages.execute import execute_stage

    mock_runner = AsyncMock(return_value=WORKER_DONE_JSON)
    ctx = _make_ctx(db_path=db_path, runner=mock_runner, max_attempts=1)

    # Patch log_run to avoid file I/O
    with patch("supervisor.run_logger.log_run"):
        await execute_stage(ctx)

    # DI runner was called
    mock_runner.assert_awaited_once()
    # Result parsed from DI runner output
    assert ctx.parsed is not None
    assert ctx.worker_status == "done"
    assert ctx.confidence == 95


@pytest.mark.asyncio
async def test_execute_stage_passes_returncode_zero(db_path):
    """BUG-007: execute_stage passes returncode=0 to log_run on success."""
    from supervisor.stages.execute import execute_stage

    mock_runner = AsyncMock(return_value=WORKER_DONE_JSON)
    ctx = _make_ctx(db_path=db_path, runner=mock_runner, max_attempts=1)

    with patch("supervisor.run_logger.log_run") as mock_log_run:
        await execute_stage(ctx)

    mock_log_run.assert_called_once()
    _, kwargs = mock_log_run.call_args
    assert kwargs["returncode"] == 0, f"Expected returncode=0, got {kwargs.get('returncode')}"


@pytest.mark.asyncio
async def test_di_runner_not_set_uses_default(db_path):
    """ctx.runner=None → execute_stage falls back to real run_claude (patched)."""
    from supervisor.stages.execute import execute_stage

    default_mock = AsyncMock(return_value=WORKER_DONE_JSON)
    ctx = _make_ctx(db_path=db_path, max_attempts=1)
    assert ctx.runner is None

    with (
        patch("supervisor.claude_runner.run_claude", default_mock),
        patch("supervisor.run_logger.log_run"),
    ):
        await execute_stage(ctx)

    # Default runner (patched via old path) was called
    default_mock.assert_awaited_once()
    assert ctx.parsed is not None
    assert ctx.worker_status == "done"


# ── test_di_executor_used ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_di_executor_used(db_path):
    """ctx.executor is set → deliver_stage uses it instead of real safe_exec."""
    from supervisor.stages.deliver import deliver_stage

    mock_executor = MagicMock(return_value=("", "", 0))
    mock_repo_mgr = MagicMock()
    mock_repo_mgr._worktree_path = MagicMock(return_value="/fake/wt")

    ctx = _make_ctx(
        db_path=db_path,
        executor=mock_executor,
        repo_manager=mock_repo_mgr,
        token="tok-123",
        worker_status="done",
        parsed={
            "status": "done",
            "confidence": 90,
            "result": {"repos": [], "notes": "test"},
        },
        repos_context=[{"alias": "api", "path": "/fake/wt"}],
        worker_cfg={
            "repos": [{"alias": "api"}],
        },
    )

    # Patch release_lease to avoid DB interaction
    with patch("supervisor.stages.deliver.release_lease"):
        await deliver_stage(ctx)

    # DI executor was called (git add, commit, push at minimum)
    assert mock_executor.call_count >= 2


@pytest.mark.asyncio
async def test_di_executor_not_set_uses_default(db_path):
    """ctx.executor=None → deliver_stage falls back to safe_exec (patched)."""
    from supervisor.stages.deliver import deliver_stage

    mock_repo_mgr = MagicMock()
    mock_repo_mgr._worktree_path = MagicMock(return_value="/fake/wt")

    ctx = _make_ctx(
        db_path=db_path,
        repo_manager=mock_repo_mgr,
        token="tok-123",
        worker_status="done",
        parsed={
            "status": "done",
            "confidence": 90,
            "result": {"repos": [], "notes": "test"},
        },
        repos_context=[{"alias": "api", "path": "/fake/wt"}],
        worker_cfg={
            "repos": [{"alias": "api"}],
        },
    )
    assert ctx.executor is None

    # Patch via old path — backward compatible
    with (
        patch(
            "supervisor.stages.deliver.safe_exec", return_value=("", "", 0)
        ) as default_mock,
        patch("supervisor.stages.deliver.release_lease"),
    ):
        await deliver_stage(ctx)

    # Old-style patching still works
    assert default_mock.call_count >= 2


# ── Event bus tests ──────────────────────────────────────────────────────────


def test_emit_and_drain_events():
    """ctx.emit records events; drain_events returns and clears them."""
    ctx = _make_ctx()
    assert ctx.drain_events() == []

    ctx.emit("task_done", confidence=90, notes="ok")
    ctx.emit("task_failed", reason="crash", message="boom")

    events = ctx.drain_events()
    assert len(events) == 2
    assert events[0] == {"type": "task_done", "data": {"confidence": 90, "notes": "ok"}}
    assert events[1] == {
        "type": "task_failed",
        "data": {"reason": "crash", "message": "boom"},
    }
    # After drain, list is empty
    assert ctx.drain_events() == []


@pytest.mark.asyncio
async def test_events_dispatched_after_stage():
    """Stage emits event -> handle_events dispatches it via tg_handler."""
    ctx = _make_ctx()

    async def stage_emitter(c):
        c.emit("task_done", confidence=95, notes="all good")

    await run_pipeline(ctx, [stage_emitter])

    ctx.tg_handler.notify_owner.assert_called_once()
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "DONE" in msg
    assert "95" in msg


@pytest.mark.asyncio
async def test_task_failed_event_on_stage_error(db_path):
    """StageError with notify=True -> task_failed event dispatched via bus."""
    from storage.db import create_task

    create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="test",
        client_contact="42",
    )

    ctx = _make_ctx(db_path=db_path, token="tok-123")

    async def stage_fail(c):
        raise WorkerCrash(reason="worker_crash", message="boom")

    with patch("supervisor.pipeline.is_lease_valid", return_value=True):
        await run_pipeline(ctx, [stage_fail])

    # Event bus dispatched the failure via tg_handler.notify_owner
    ctx.tg_handler.notify_owner.assert_called_once()
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "boom" in msg


@pytest.mark.asyncio
async def test_multiple_events_in_single_stage():
    """Stage emits multiple events -> all dispatched after stage."""
    ctx = _make_ctx()

    async def stage_multi(c):
        c.emit("review_approved", message="APPROVED ok")
        c.emit("task_done", confidence=80, notes="done")

    await run_pipeline(ctx, [stage_multi])

    assert ctx.tg_handler.notify_owner.call_count == 2


@pytest.mark.asyncio
async def test_events_dispatched_per_stage():
    """Events from stage_a dispatched before stage_b runs."""
    ctx = _make_ctx()
    dispatch_order = []

    original_notify = ctx.tg_handler.notify_owner

    async def tracking_notify(msg):
        dispatch_order.append(msg)
        return await original_notify(msg)

    ctx.tg_handler.notify_owner = AsyncMock(side_effect=tracking_notify)

    async def stage_a(c):
        c.emit("task_done", confidence=90, notes="a done")

    async def stage_b(c):
        # By the time stage_b runs, stage_a events should already be dispatched
        assert len(dispatch_order) == 1

    await run_pipeline(ctx, [stage_a, stage_b])


@pytest.mark.asyncio
async def test_unknown_event_type_logged_no_crash():
    """Unknown event type is logged but pipeline does not crash."""
    ctx = _make_ctx()

    async def stage_unknown(c):
        c.emit("nonexistent_event_type", foo="bar")

    # Should not raise
    await run_pipeline(ctx, [stage_unknown])

    # tg_handler.notify_owner should NOT be called for unknown events
    ctx.tg_handler.notify_owner.assert_not_called()


# ── test_pipeline_aborts_on_lost_lease (BUG-017) ──────────────────────────


@pytest.mark.asyncio
async def test_pipeline_aborts_on_lost_lease():
    """BUG-017: pipeline aborts if lease is lost before a stage."""
    ctx = _make_ctx(token="tok-lost")
    call_order = []

    async def stage_a(c):
        call_order.append("a")

    async def stage_b(c):
        call_order.append("b")

    # is_lease_valid returns False → pipeline should abort before stage_b
    with patch("supervisor.pipeline.is_lease_valid", return_value=False):
        await run_pipeline(ctx, [stage_a, stage_b])

    # stage_a not called because lease check happens before each stage
    assert call_order == []


@pytest.mark.asyncio
async def test_pipeline_continues_with_valid_lease():
    """Pipeline continues normally when lease is valid."""
    ctx = _make_ctx(token="tok-valid")
    call_order = []

    async def stage_a(c):
        call_order.append("a")

    async def stage_b(c):
        call_order.append("b")

    with patch("supervisor.pipeline.is_lease_valid", return_value=True):
        await run_pipeline(ctx, [stage_a, stage_b])

    assert call_order == ["a", "b"]


# ── test_background_renewal_exception_resilience (BUG-026) ────────────────


@pytest.mark.asyncio
async def test_background_renewal_survives_exception():
    """BUG-026: background renewal continues after renew_lease exception."""
    import asyncio
    from supervisor.pipeline import _background_lease_renewal

    ctx = _make_ctx(token="tok-bg", lease_ttl=60)
    stop = asyncio.Event()
    call_count = {"n": 0}

    def mock_renew(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("db locked")
        return True

    with patch("supervisor.pipeline.renew_lease", side_effect=mock_renew):
        task = asyncio.create_task(
            _background_lease_renewal(ctx, stop, _interval=0.1)
        )
        await asyncio.sleep(0.5)
        stop.set()
        await task

    assert call_count["n"] >= 2, f"Expected >=2 calls, got {call_count['n']}"


@pytest.mark.asyncio
async def test_background_renewal_actually_renews():
    """BUG-026: verify background task actually calls renew_lease."""
    import asyncio
    from supervisor.pipeline import _background_lease_renewal

    ctx = _make_ctx(token="tok-active", lease_ttl=60)
    stop = asyncio.Event()
    renew_calls = []

    def mock_renew(*args, **kwargs):
        renew_calls.append(1)
        return True

    with patch("supervisor.pipeline.renew_lease", side_effect=mock_renew):
        task = asyncio.create_task(
            _background_lease_renewal(ctx, stop, _interval=0.05)
        )
        await asyncio.sleep(0.3)
        stop.set()
        await task

    assert len(renew_calls) >= 3, f"Expected >=3 renewals, got {len(renew_calls)}"


@pytest.mark.asyncio
async def test_background_renewal_skips_when_no_token():
    """Background renewal skips renew_lease when token not yet set."""
    import asyncio
    from supervisor.pipeline import _background_lease_renewal

    ctx = _make_ctx(lease_ttl=60)
    ctx.token = ""
    stop = asyncio.Event()

    with patch("supervisor.pipeline.renew_lease") as mock_renew:
        task = asyncio.create_task(
            _background_lease_renewal(ctx, stop, _interval=0.05)
        )
        await asyncio.sleep(0.2)
        stop.set()
        await task

    mock_renew.assert_not_called()


@pytest.mark.asyncio
async def test_check_lease_or_abort_returns_true_when_valid():
    """ctx.check_lease_or_abort() returns True when lease is valid."""
    ctx = _make_ctx(token="tok-valid")
    with patch("supervisor.pipeline.is_lease_valid", return_value=True):
        result = await ctx.check_lease_or_abort()
    assert result is True


@pytest.mark.asyncio
async def test_check_lease_or_abort_returns_false_when_lost():
    """ctx.check_lease_or_abort() returns False when lease is gone."""
    ctx = _make_ctx(token="tok-gone")
    with patch("supervisor.pipeline.is_lease_valid", return_value=False):
        result = await ctx.check_lease_or_abort()
    assert result is False
