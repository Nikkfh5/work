"""
tests/test_deliver_stage.py — Tests for CI auto-fix loop in deliver_stage.

Covers:
- CI passes first try (no auto-fix triggered)
- CI fails, auto-fix succeeds on retry
- CI fails all attempts -> requires_manual
- ci_auto_fix_attempts=1 behaves like old code (no auto-fix loop)
- Runner crash during auto-fix -> falls through to failure
- _build_ci_fix_prompt output correctness
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from supervisor.pipeline import WorkerContext
from supervisor.stages.deliver import _build_ci_fix_prompt, deliver_stage


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_ctx(**overrides) -> WorkerContext:
    """Create WorkerContext with deliver-friendly defaults."""
    defaults = dict(
        task_id="test-task-123456",
        worker_id="job1_worker",
        task_description="implement feature X",
        config={"supervisor": {}, "workers": {"job1_worker": {}}},
        tg_handler=AsyncMock(),
        db_path=None,
        worker_cfg={"repos": [{"alias": "api"}]},
        job="job1",
        worker_dir="workers/job1_worker",
        worker_status="done",
        parsed={
            "status": "done",
            "confidence": 90,
            "result": {"repos": [], "notes": "test"},
        },
        repos_context=[{"alias": "api", "path": "/fake/wt"}],
        repo_manager=MagicMock(),
        token="tok-123",
        branch="ai/task-test",
    )
    defaults.update(overrides)
    ctx = WorkerContext(**defaults)
    ctx.repo_manager._worktree_path = MagicMock(return_value="/fake/wt")
    return ctx


# ── test_ci_passes_first_try ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ci_passes_first_try(db_path):
    """CI succeeds on the first attempt — no auto-fix triggered."""
    mock_executor = MagicMock(return_value=("", "", 0))
    ctx = _make_ctx(db_path=db_path, executor=mock_executor)

    with patch("supervisor.stages.deliver.release_lease"):
        await deliver_stage(ctx)

    # No ci_auto_fix events emitted
    events = ctx.drain_events()
    ci_fix_events = [e for e in events if e["type"] == "ci_auto_fix"]
    assert len(ci_fix_events) == 0

    # Task completed (task_done emitted)
    done_events = [e for e in events if e["type"] == "task_done"]
    assert len(done_events) == 1


# ── test_ci_auto_fix_success_on_retry ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ci_auto_fix_success_on_retry(db_path):
    """CI fails first, auto-fix runs, second CI passes."""
    call_count = {"n": 0}

    def mock_executor(cmd, cwd=None, timeout=None):
        """First CI run fails (git push rc=1), second succeeds."""
        if cmd == ["git", "push", "origin", "HEAD"]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return ("", "rejected", 1)  # fail first push
            return ("", "", 0)  # succeed second push
        return ("", "", 0)  # everything else OK

    mock_runner = AsyncMock(return_value='{"status":"done"}')
    ctx = _make_ctx(
        db_path=db_path,
        executor=mock_executor,
        runner=mock_runner,
    )

    with patch("supervisor.stages.deliver.release_lease"):
        await deliver_stage(ctx)

    # Auto-fix runner was called once (between attempt 1 and 2)
    mock_runner.assert_awaited_once()

    # ci_auto_fix event emitted
    events = ctx.drain_events()
    ci_fix_events = [e for e in events if e["type"] == "ci_auto_fix"]
    assert len(ci_fix_events) == 1
    assert ci_fix_events[0]["data"]["attempt"] == 1

    # Task completed successfully
    done_events = [e for e in events if e["type"] == "task_done"]
    assert len(done_events) == 1


# ── test_ci_auto_fix_exhausted ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ci_auto_fix_exhausted(db_path):
    """CI fails all 3 attempts -> requires_manual."""

    def mock_executor(cmd, cwd=None, timeout=None):
        """git push always fails."""
        if cmd == ["git", "push", "origin", "HEAD"]:
            return ("", "rejected: non-fast-forward", 1)
        return ("", "", 0)

    mock_runner = AsyncMock(return_value='{"status":"done"}')
    ctx = _make_ctx(
        db_path=db_path,
        executor=mock_executor,
        runner=mock_runner,
        worker_cfg={"repos": [{"alias": "api"}], "ci_auto_fix_attempts": 3},
    )

    with patch("supervisor.stages.deliver._fail_final") as mock_fail:
        await deliver_stage(ctx)

    # _fail_final called once
    mock_fail.assert_called_once()

    # Runner called 2 times (after attempt 1, after attempt 2; attempt 3 is last so no fix)
    assert mock_runner.await_count == 2

    # task_failed emitted with correct message
    events = ctx.drain_events()
    fail_events = [e for e in events if e["type"] == "task_failed"]
    assert len(fail_events) == 1
    assert "auto-fix attempts" in fail_events[0]["data"]["message"]

    # ci_auto_fix emitted twice (attempts 1 and 2)
    ci_fix_events = [e for e in events if e["type"] == "ci_auto_fix"]
    assert len(ci_fix_events) == 2


# ── test_ci_auto_fix_single_attempt_no_loop ─────────────────────────────────


@pytest.mark.asyncio
async def test_ci_auto_fix_single_attempt_no_loop(db_path):
    """ci_auto_fix_attempts=1 means no auto-fix loop (old behavior)."""

    def mock_executor(cmd, cwd=None, timeout=None):
        if cmd == ["git", "push", "origin", "HEAD"]:
            return ("", "push failed", 1)
        return ("", "", 0)

    mock_runner = AsyncMock(return_value='{"status":"done"}')
    ctx = _make_ctx(
        db_path=db_path,
        executor=mock_executor,
        runner=mock_runner,
        worker_cfg={"repos": [{"alias": "api"}], "ci_auto_fix_attempts": 1},
    )

    with patch("supervisor.stages.deliver._fail_final") as mock_fail:
        await deliver_stage(ctx)

    # Runner never called — single attempt means no retry
    mock_runner.assert_not_awaited()

    # Failure emitted
    mock_fail.assert_called_once()
    events = ctx.drain_events()
    fail_events = [e for e in events if e["type"] == "task_failed"]
    assert len(fail_events) == 1


# ── test_ci_auto_fix_runner_crash ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_ci_auto_fix_runner_crash(db_path):
    """If runner crashes during auto-fix, loop breaks and task fails."""

    def mock_executor(cmd, cwd=None, timeout=None):
        if cmd == ["git", "push", "origin", "HEAD"]:
            return ("", "push error", 1)
        return ("", "", 0)

    mock_runner = AsyncMock(side_effect=RuntimeError("runner exploded"))
    ctx = _make_ctx(
        db_path=db_path,
        executor=mock_executor,
        runner=mock_runner,
        worker_cfg={"repos": [{"alias": "api"}], "ci_auto_fix_attempts": 3},
    )

    with patch("supervisor.stages.deliver._fail_final") as mock_fail:
        await deliver_stage(ctx)

    # Runner was called once then crashed -> loop broken
    mock_runner.assert_awaited_once()

    # Task failed
    mock_fail.assert_called_once()

    # No ci_auto_fix events (crash happened before emit)
    events = ctx.drain_events()
    ci_fix_events = [e for e in events if e["type"] == "ci_auto_fix"]
    assert len(ci_fix_events) == 0


# ── test_build_ci_fix_prompt ────────────────────────────────────────────────


def test_build_ci_fix_prompt_basic():
    """_build_ci_fix_prompt includes task description, error, and attempt info."""
    prompt = _build_ci_fix_prompt(
        task_description="implement login",
        ci_error="ruff check failed: E501 line too long",
        attempt=1,
        max_attempts=3,
    )
    assert "implement login" in prompt
    assert "ruff check failed" in prompt
    assert "1/3" in prompt
    assert "<<<JSON>>>" in prompt
    assert "<<<END>>>" in prompt


def test_build_ci_fix_prompt_with_repos_context():
    """_build_ci_fix_prompt includes workspace section when repos_context given."""
    prompt = _build_ci_fix_prompt(
        task_description="fix tests",
        ci_error="pytest failed",
        attempt=2,
        max_attempts=3,
        repos_context=[{"alias": "api", "path": "/srv/api"}],
        branch="ai/task-abc",
    )
    assert "ai/task-abc" in prompt
    assert "api" in prompt
    assert "/srv/api" in prompt
    assert "2/3" in prompt


def test_build_ci_fix_prompt_truncates():
    """Long task_description and ci_error are truncated."""
    prompt = _build_ci_fix_prompt(
        task_description="x" * 500,
        ci_error="e" * 2000,
        attempt=1,
        max_attempts=2,
    )
    # task_description truncated to 200 chars
    assert "x" * 200 in prompt
    assert "x" * 201 not in prompt
    # ci_error truncated to 1000 chars
    assert "e" * 1000 in prompt
    assert "e" * 1001 not in prompt


# ── test_ci_auto_fix_event_handler ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_ci_auto_fix_event_dispatched():
    """ci_auto_fix event is dispatched via event_handlers."""
    from supervisor.event_handlers import _dispatch_event

    ctx = _make_ctx()
    event = {"type": "ci_auto_fix", "data": {"attempt": 2, "error": "lint failed"}}
    await _dispatch_event(ctx, event)

    ctx.tg_handler.notify_owner.assert_called_once()
    msg = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "CI failed" in msg
    assert "attempt 2" in msg
    assert "lint failed" in msg
