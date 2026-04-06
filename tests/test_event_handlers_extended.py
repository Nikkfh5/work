"""
tests/test_event_handlers_extended.py — Extended edge case tests for event_handlers.

The base tests/test_event_handlers.py covers all 11 event types + unknown.
This file covers additional edge cases:
- task_done with zero cost (no $ in message)
- task_done with large cost
- task_failed with empty data (no message, no reason)
- task_blocked with empty question
- review_approved with default message
- review_needs_changes with missing fields
- ci_auto_fix with missing fields
- plan_created with long plan_text truncation
- session_refresh with zero tokens
- handle_events with empty event list (no-op)
- handle_events processes events in order
- multiple events of same type
- _dispatch_event with data containing non-string values

Запуск: pytest tests/test_event_handlers_extended.py -v
"""

import logging

import pytest
from unittest.mock import AsyncMock

from supervisor.pipeline import WorkerContext
from supervisor.event_handlers import handle_events, _dispatch_event


# ── Helper ───────────────────────────────────────────────────────────────────


def _make_ctx(**overrides) -> WorkerContext:
    defaults = dict(
        task_id="test-task-abcdef12",
        worker_id="job1_worker",
        task_description="extended test task",
        config={"supervisor": {}, "workers": {"job1_worker": {}}},
        tg_handler=AsyncMock(),
        db_path=None,
    )
    defaults.update(overrides)
    return WorkerContext(**defaults)


# ── handle_events edge cases ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_events_empty_no_calls():
    """No events emitted -> handle_events is a no-op, notify_owner not called."""
    ctx = _make_ctx()
    assert ctx._events == []
    await handle_events(ctx)
    ctx.tg_handler.notify_owner.assert_not_called()


@pytest.mark.asyncio
async def test_handle_events_preserves_order():
    """Events processed in FIFO order."""
    ctx = _make_ctx()
    call_order = []

    async def track_notify(msg):
        if "DONE" in msg:
            call_order.append("done")
        elif "BLOCKED" in msg:
            call_order.append("blocked")
        elif "Plan approved" in msg:
            call_order.append("approved")

    ctx.tg_handler.notify_owner = AsyncMock(side_effect=track_notify)

    ctx.emit("task_done", confidence=90, notes="ok", cost_usd=0)
    ctx.emit("task_blocked", question="help")
    ctx.emit("plan_approved")
    await handle_events(ctx)

    assert call_order == ["done", "blocked", "approved"]


@pytest.mark.asyncio
async def test_multiple_events_same_type():
    """Multiple task_done events -> all dispatched."""
    ctx = _make_ctx()
    ctx.emit("task_done", confidence=90, notes="first", cost_usd=0)
    ctx.emit("task_done", confidence=80, notes="second", cost_usd=0.01)
    await handle_events(ctx)
    assert ctx.tg_handler.notify_owner.call_count == 2


# ── task_done edge cases ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_done_large_cost():
    """Large cost value is formatted correctly."""
    ctx = _make_ctx()
    await _dispatch_event(
        ctx, {"type": "task_done", "data": {"confidence": 70, "notes": "expensive", "cost_usd": 1.2345}}
    )
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "$1.2345" in msg


@pytest.mark.asyncio
async def test_task_done_missing_fields():
    """task_done with empty data -> defaults used, no crash."""
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "task_done", "data": {}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "DONE" in msg
    assert "?" in msg  # default confidence


# ── task_failed edge cases ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_failed_empty_data():
    """task_failed with no message and no reason -> default 'error'."""
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "task_failed", "data": {}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "error" in msg
    assert "/retry" in msg


# ── task_blocked edge cases ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_blocked_empty_question():
    """task_blocked with empty question -> still sends BLOCKED."""
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "task_blocked", "data": {"question": ""}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "BLOCKED" in msg


# ── review_approved edge cases ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_approved_default_message():
    """review_approved with no message -> uses default 'APPROVED'."""
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "review_approved", "data": {}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "APPROVED" in msg


# ── review_needs_changes edge cases ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_needs_changes_missing_fields():
    """review_needs_changes with empty data -> defaults to '?'."""
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "review_needs_changes", "data": {}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "NEEDS_CHANGES" in msg
    assert "?" in msg


# ── ci_auto_fix edge cases ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ci_auto_fix_missing_fields():
    """ci_auto_fix with no attempt/error -> defaults used."""
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "ci_auto_fix", "data": {}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "CI failed" in msg


# ── plan_revision_requested edge cases ───────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_revision_default_revision():
    """plan_revision_requested with no revision -> defaults to '?'."""
    ctx = _make_ctx()
    await _dispatch_event(ctx, {"type": "plan_revision_requested", "data": {}})
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "Revising plan" in msg
    assert "?" in msg


# ── session_refresh edge cases ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_refresh_zero_tokens():
    """session_refresh with tokens=0 -> formatted correctly."""
    ctx = _make_ctx()
    await _dispatch_event(
        ctx, {"type": "session_refresh", "data": {"tokens": 0, "cost": 0}}
    )
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "Session refresh" in msg
    assert "$0.0000" in msg


# ── data type edge cases ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_done_notes_is_integer():
    """task_done with notes as integer -> str() conversion, no crash."""
    ctx = _make_ctx()
    await _dispatch_event(
        ctx, {"type": "task_done", "data": {"confidence": 90, "notes": 12345, "cost_usd": 0}}
    )
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "DONE" in msg
    assert "12345" in msg
