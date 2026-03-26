"""
tests/test_health_monitor.py — тесты для supervisor/health_monitor.py.

Покрытие:
- build_snapshot: с задачами и на пустой DB
- run_health_check: GREEN/RED сценарии, TG alerting
- auto_actions: cleanup_worktrees, release_stale_leases
- create_tasks: задачи в DB со статусом pending_approval
- invalid JSON: warning + return None
- _get_dir_size / _format_bytes: утилиты
"""

import pytest
from unittest.mock import patch, MagicMock

from storage.db import get_conn
from supervisor.health_monitor import (
    _format_bytes,
    _get_dir_size,
    build_snapshot,
    run_health_check,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _insert_task(
    db_path, task_id, status="pending", description="test task", error=None
):
    """Insert a task directly for testing."""
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO tasks (id, status, source, source_contact, assigned_worker,
               description, client_contact, priority, last_error_reason)
               VALUES (?, ?, 'test', 'test@test', 'job1_worker', ?, 'test@test', 'normal', ?)""",
            (task_id, status, description, error),
        )


def _make_green_json():
    """Valid GREEN health JSON output."""
    return """Some preamble text
<<<JSON>>>
{
  "status": "done",
  "confidence": 95,
  "health": {
    "overall": "GREEN",
    "findings": [],
    "auto_actions": [],
    "create_tasks": [],
    "tg_alert": "",
    "notion_detail": ""
  },
  "question": null
}
<<<END>>>
trailing text"""


def _make_red_json():
    """Valid RED health JSON output with findings and actions."""
    return """
<<<JSON>>>
{
  "status": "done",
  "confidence": 85,
  "health": {
    "overall": "RED",
    "findings": [
      {
        "severity": "high",
        "title": "Stale leases detected",
        "evidence": "3 tasks stuck in running state",
        "suggestion": "Release stale leases"
      }
    ],
    "auto_actions": ["release_stale_leases"],
    "create_tasks": [
      {
        "title": "Investigate worker crashes",
        "summary": "Multiple workers failing with E_WORKER_CRASH",
        "priority": "P1"
      }
    ],
    "tg_alert": "3 tasks stuck, leases released",
    "notion_detail": "## Critical: Stale leases\\n3 tasks..."
  },
  "question": null
}
<<<END>>>"""


def _make_yellow_json_with_cleanup():
    """Valid YELLOW health JSON with cleanup_worktrees action."""
    return """
<<<JSON>>>
{
  "status": "done",
  "confidence": 80,
  "health": {
    "overall": "YELLOW",
    "findings": [
      {
        "severity": "med",
        "title": "Old worktrees accumulating",
        "evidence": "worktrees/ is 2.5 GB",
        "suggestion": "Run cleanup_worktrees"
      }
    ],
    "auto_actions": ["cleanup_worktrees"],
    "create_tasks": [],
    "tg_alert": "Disk usage growing — cleanup triggered",
    "notion_detail": ""
  },
  "question": null
}
<<<END>>>"""


# ── Test _get_dir_size ───────────────────────────────────────────────────────


def test_dir_size_helper(tmp_path):
    """_get_dir_size returns total bytes of all files recursively."""
    # Create some files
    (tmp_path / "a.txt").write_text("hello")  # 5 bytes
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world!")  # 6 bytes

    size = _get_dir_size(str(tmp_path))
    assert size == 11


def test_dir_size_nonexistent(tmp_path):
    """_get_dir_size returns 0 for nonexistent directory."""
    size = _get_dir_size(str(tmp_path / "nope"))
    assert size == 0


# ── Test _format_bytes ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "size,expected",
    [
        (0, "0.0 B"),
        (512, "512.0 B"),
        (1024, "1.0 KB"),
        (1048576, "1.0 MB"),
        (1073741824, "1.0 GB"),
        (1099511627776, "1.0 TB"),
        (-5, "0.0 B"),
    ],
)
def test_format_bytes(size, expected):
    """_format_bytes formats sizes correctly."""
    assert _format_bytes(size) == expected


# ── Test build_snapshot ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_snapshot_with_tasks(db_path):
    """build_snapshot includes task statistics from DB."""
    _insert_task(db_path, "t1", status="done", description="task 1")
    _insert_task(db_path, "t2", status="done", description="task 2")
    _insert_task(db_path, "t3", status="running", description="task 3")
    _insert_task(
        db_path, "t4", status="error", description="task 4", error="worker_crash"
    )

    snapshot = await build_snapshot(db_path=db_path)

    assert "Task Statistics" in snapshot
    assert "done: 2" in snapshot
    assert "running: 1" in snapshot
    assert "error: 1" in snapshot


@pytest.mark.asyncio
async def test_build_snapshot_empty_db(db_path):
    """build_snapshot works with empty DB — no errors."""
    snapshot = await build_snapshot(db_path=db_path)

    assert "Task Statistics" in snapshot
    assert "(no tasks)" in snapshot
    assert "(no errors)" in snapshot
    assert "System Health Snapshot" in snapshot


# ── Test run_health_check — GREEN ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_health_check_green(db_path, mock_tg_handler):
    """GREEN result: no TG alert sent, returns parsed dict."""

    async def fake_runner(prompt, cwd):
        return _make_green_json()

    result = await run_health_check(
        tg_handler=mock_tg_handler,
        db_path=db_path,
        config={},
        runner=fake_runner,
    )

    assert result is not None
    assert result["health"]["overall"] == "GREEN"
    # GREEN => no TG alert
    mock_tg_handler.notify_owner.assert_not_awaited()


# ── Test run_health_check — RED ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_health_check_red(db_path, mock_tg_handler):
    """RED result: TG alert sent, auto_actions executed, tasks created."""

    async def fake_runner(prompt, cwd):
        return _make_red_json()

    with patch(
        "supervisor.health_monitor.release_stale", return_value=2
    ) as mock_release:
        result = await run_health_check(
            tg_handler=mock_tg_handler,
            db_path=db_path,
            config={},
            runner=fake_runner,
        )

    assert result is not None
    assert result["health"]["overall"] == "RED"

    # RED => TG alert sent
    mock_tg_handler.notify_owner.assert_awaited_once()
    alert_text = mock_tg_handler.notify_owner.call_args[0][0]
    assert "RED" in alert_text

    # auto_action release_stale_leases was called
    mock_release.assert_called_once()

    # create_tasks => task in DB with pending_approval
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'pending_approval'"
        ).fetchall()
    assert len(rows) == 1
    task = dict(rows[0])
    assert "Investigate worker crashes" in task["description"]
    assert task["priority"] == "urgent"  # P1 -> urgent


# ── Test auto_action: cleanup_worktrees ──────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_action_cleanup_worktrees(db_path, mock_tg_handler):
    """auto_action 'cleanup_worktrees' calls RepoManager.cleanup_old_worktrees."""

    async def fake_runner(prompt, cwd):
        return _make_yellow_json_with_cleanup()

    with patch("supervisor.health_monitor.RepoManager") as MockRM:
        mock_instance = MagicMock()
        mock_instance.cleanup_old_worktrees.return_value = 5
        MockRM.return_value = mock_instance

        result = await run_health_check(
            tg_handler=mock_tg_handler,
            db_path=db_path,
            config={},
            runner=fake_runner,
        )

    assert result is not None
    mock_instance.cleanup_old_worktrees.assert_called_once()


# ── Test auto_action: release_stale_leases ───────────────────────────────────


@pytest.mark.asyncio
async def test_auto_action_release_stale(db_path, mock_tg_handler):
    """auto_action 'release_stale_leases' calls lease_manager.release_stale."""

    async def fake_runner(prompt, cwd):
        return _make_red_json()

    with patch(
        "supervisor.health_monitor.release_stale", return_value=3
    ) as mock_release:
        result = await run_health_check(
            tg_handler=mock_tg_handler,
            db_path=db_path,
            config={},
            runner=fake_runner,
        )

    assert result is not None
    mock_release.assert_called_once_with(db_path=db_path)


# ── Test create_tasks from health ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_tasks_from_health(db_path, mock_tg_handler):
    """create_tasks from health agent creates tasks with pending_approval status."""

    health_json = """
<<<JSON>>>
{
  "status": "done",
  "confidence": 90,
  "health": {
    "overall": "YELLOW",
    "findings": [
      {"severity": "med", "title": "Test finding", "evidence": "data", "suggestion": "fix"}
    ],
    "auto_actions": [],
    "create_tasks": [
      {"title": "Fix config", "summary": "Config drift detected", "priority": "P2"},
      {"title": "Update docs", "summary": "Docs outdated", "priority": "P3"}
    ],
    "tg_alert": "2 tasks created",
    "notion_detail": ""
  },
  "question": null
}
<<<END>>>"""

    async def fake_runner(prompt, cwd):
        return health_json

    result = await run_health_check(
        tg_handler=mock_tg_handler,
        db_path=db_path,
        config={},
        runner=fake_runner,
    )

    assert result is not None

    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'pending_approval' ORDER BY created_at"
        ).fetchall()

    assert len(rows) == 2
    tasks = [dict(r) for r in rows]
    assert "Fix config" in tasks[0]["description"]
    assert tasks[0]["priority"] == "high"  # P2 -> high
    assert "Update docs" in tasks[1]["description"]
    assert tasks[1]["priority"] == "normal"  # P3 -> normal


# ── Test invalid JSON ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_health_json(db_path, mock_tg_handler):
    """Invalid JSON output => log warning, return None."""

    async def fake_runner(prompt, cwd):
        return "This is not valid JSON at all, just plain text output"

    result = await run_health_check(
        tg_handler=mock_tg_handler,
        db_path=db_path,
        config={},
        runner=fake_runner,
    )

    assert result is None
    mock_tg_handler.notify_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_health_schema(db_path, mock_tg_handler):
    """Valid JSON but invalid health schema => return None."""

    async def fake_runner(prompt, cwd):
        return '<<<JSON>>>{"status": "done", "confidence": 90}<<<END>>>'

    result = await run_health_check(
        tg_handler=mock_tg_handler,
        db_path=db_path,
        config={},
        runner=fake_runner,
    )

    assert result is None
