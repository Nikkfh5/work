"""
tests/test_event_handlers.py — Tests for supervisor/event_handlers.py.

All 11 event types + unknown event type.
"""

import logging

import pytest
from unittest.mock import AsyncMock

from supervisor.pipeline import WorkerContext
from supervisor.event_handlers import handle_events, _dispatch_event


def _make_ctx(**overrides) -> WorkerContext:
    defaults = dict(
        task_id="test-task-12345678",
        worker_id="job1_worker",
        task_description="test task",
        config={"supervisor": {}, "workers": {"job1_worker": {}}},
        tg_handler=AsyncMock(),
        db_path=None,
    )
    defaults.update(overrides)
    return WorkerContext(**defaults)


# ── handle_events ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_events_drains_all():
    """handle_events processes all accumulated events and drains the list."""
    ctx = _make_ctx()
    ctx.emit("task_done", confidence=90, notes="ok", cost_usd=0)
    ctx.emit("task_failed", message="err")
    assert len(ctx._events) == 2

    await handle_events(ctx)

    assert ctx._events == []
    assert ctx.tg_handler.notify_owner.call_count == 2


# ── Individual event types ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_done_with_cost():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "task_done", "data": {"confidence": 95, "notes": "all ok", "cost_usd": 0.05}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "DONE" in msg
    assert "95" in msg
    assert "$0.0500" in msg


@pytest.mark.asyncio
async def test_task_done_no_cost():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "task_done", "data": {"confidence": 80, "notes": "ok", "cost_usd": 0}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "DONE" in msg
    assert "$" not in msg


@pytest.mark.asyncio
async def test_task_failed_message():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "task_failed", "data": {"message": "timeout in worker"}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "timeout in worker" in msg
    assert "/retry" in msg


@pytest.mark.asyncio
async def test_task_failed_reason_fallback():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "task_failed", "data": {"reason": "crash"}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "crash" in msg


@pytest.mark.asyncio
async def test_task_blocked():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "task_blocked", "data": {"question": "Need API key"}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "BLOCKED" in msg
    assert "Need API key" in msg


@pytest.mark.asyncio
async def test_review_approved():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "review_approved", "data": {"message": "LGTM"}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "LGTM" in msg


@pytest.mark.asyncio
async def test_review_needs_changes():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "review_needs_changes", "data": {"iteration": 2, "feedback": "fix tests"}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "NEEDS_CHANGES" in msg
    assert "iter=2" in msg
    assert "fix tests" in msg


@pytest.mark.asyncio
async def test_escalation_created():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "escalation_created", "data": {"question": "DB schema unclear"}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "DB schema unclear" in msg
    assert "/retry" in msg


@pytest.mark.asyncio
async def test_ci_auto_fix():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "ci_auto_fix", "data": {"attempt": 2, "error": "lint failed"}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "CI failed" in msg
    assert "attempt 2" in msg
    assert "lint failed" in msg


@pytest.mark.asyncio
async def test_plan_created():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "plan_created", "data": {"plan_text": "Step 1: build\nStep 2: test"}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "Step 1: build" in msg


@pytest.mark.asyncio
async def test_plan_approved():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "plan_approved", "data": {}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "Plan approved" in msg


@pytest.mark.asyncio
async def test_plan_revision_requested():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "plan_revision_requested", "data": {"revision": 3}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "Revising plan" in msg
    assert "revision 3" in msg


@pytest.mark.asyncio
async def test_session_refresh():
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "session_refresh", "data": {"tokens": 800000, "cost": 0.50}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "Session refresh" in msg
    assert "800000" in msg


@pytest.mark.asyncio
async def test_unknown_event_type_no_crash(caplog):
    ctx = _make_ctx()
    with caplog.at_level(logging.WARNING):
        await _dispatch_event(ctx, {"type": "alien_invasion", "data": {"ships": 42}})
    assert "Unknown event type" in caplog.text
    ctx.tg_handler.notify_owner.assert_not_called()
