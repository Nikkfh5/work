"""
tests/test_summarizer.py — tests for supervisor/summarizer.py.

6 tests:
  1. test_no_tasks_returns_none — empty DB -> None
  2. test_tasks_done_included — 2 done tasks -> "Завершено: 2"
  3. test_tasks_blocked_included — 1 blocked -> "Заблокировано: 1"
  4. test_errors_listed — 2 tasks with worker_crash -> "worker_crash: 2"
  5. test_run_daily_summary_sends_tg — tasks exist -> notify_owner called
  6. test_run_daily_summary_skips_empty — no tasks -> notify_owner NOT called
"""

import pytest

from storage.db import create_task, get_conn
from supervisor.summarizer import build_daily_summary, run_daily_summary


# ── helpers ─────────────────────────────────────────────────────────────────


def _create_task_with_status(
    db_path: str, status: str, error: str | None = None
) -> str:
    """Create a task and immediately set its status (+ optional error)."""
    task_id = create_task(
        source="telegram",
        source_contact="@test",
        assigned_worker="w1",
        description="test task",
        client_contact="@test",
    )
    with get_conn(db_path) as conn:
        if error:
            conn.execute(
                """UPDATE tasks
                   SET status = ?, last_error_reason = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (status, error, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, task_id),
            )
    return task_id


# ── 1. no tasks -> None ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_tasks_returns_none(db_path):
    """Empty DB -> build_daily_summary returns None."""
    result = await build_daily_summary(db_path)
    assert result is None


# ── 2. done tasks counted ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tasks_done_included(db_path):
    """Two done tasks -> summary contains 'Завершено: 2'."""
    _create_task_with_status(db_path, "done")
    _create_task_with_status(db_path, "done")

    result = await build_daily_summary(db_path)
    assert result is not None
    assert "Завершено: 2" in result


# ── 3. blocked tasks counted ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tasks_blocked_included(db_path):
    """One blocked task -> summary contains 'Заблокировано: 1'."""
    _create_task_with_status(db_path, "blocked")

    result = await build_daily_summary(db_path)
    assert result is not None
    assert "Заблокировано: 1" in result


# ── 4. errors listed ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_errors_listed(db_path):
    """Two tasks with worker_crash -> 'worker_crash: 2' in summary."""
    _create_task_with_status(db_path, "blocked", error="worker_crash")
    _create_task_with_status(db_path, "blocked", error="worker_crash")

    result = await build_daily_summary(db_path)
    assert result is not None
    assert "worker_crash: 2" in result


# ── 5. run_daily_summary sends TG ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_daily_summary_sends_tg(db_path, mock_tg_handler):
    """Tasks exist -> tg_handler.notify_owner is called with summary text."""
    _create_task_with_status(db_path, "done")

    await run_daily_summary(mock_tg_handler, db_path)

    mock_tg_handler.notify_owner.assert_awaited_once()
    sent_text = mock_tg_handler.notify_owner.call_args[0][0]
    assert "Завершено: 1" in sent_text


# ── 6. run_daily_summary skips empty ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_daily_summary_skips_empty(db_path, mock_tg_handler):
    """No tasks -> notify_owner is NOT called."""
    await run_daily_summary(mock_tg_handler, db_path)

    mock_tg_handler.notify_owner.assert_not_awaited()
