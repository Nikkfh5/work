"""
tests/test_review_cycle.py — тесты для review iteration flow.

Запуск: pytest tests/test_review_cycle.py -v

Тестируем:
- Reviewer APPROVED → CI pass → push → done
- Reviewer NEEDS_CHANGES → worker retry → APPROVED → done
- Reviewer exhausted iterations → requires_manual
- No reviewer configured → skip review (current behaviour)
- CI failure blocks push → requires_manual
- Reviewer invalid JSON → requires_manual
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from storage.db import get_conn, create_task
from tests.conftest import WORKER_DONE_JSON


# ── Фикстуры ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_repo_manager():
    """Мок RepoManager с worktree path."""
    from pathlib import Path

    mgr = MagicMock()
    mgr.ensure_mirror = MagicMock(return_value=Path("/fake/mirror"))
    mgr.prepare_worktree = MagicMock(return_value=Path("/fake/worktree"))
    mgr.cleanup_worktree = MagicMock()
    mgr._worktree_path = MagicMock(return_value=Path("/fake/worktree/api"))
    return mgr


def _make_task(db_path, worker_id="job1_worker"):
    """Создать задачу в DB и вернуть dict."""
    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker=worker_id,
        description="implement feature X",
        client_contact="42",
    )
    return {
        "id": task_id,
        "assigned_worker": worker_id,
        "description": "implement feature X",
    }


def _config_with_reviewer():
    """Конфиг с reviewer_id для review cycle тестов."""
    return {
        "supervisor": {"confidence_threshold": 70},
        "workers": {
            "job1_worker": {
                "max_attempts": 1,
                "reviewer_id": "job1_reviewer",
                "max_review_iterations": 3,
                "repos": [
                    {
                        "alias": "api",
                        "url": "https://github.com/org/api",
                        "token_env": "GIT_TOKEN_JOB1",
                        "clone_strategy": "shallow",
                    }
                ],
                "branching_policy": {
                    "pattern": "ai/task-{task_id}",
                    "base": "main",
                },
                "ci_policy": {
                    "run_before_push": [["pytest"], ["ruff", "check", "."]],
                    "required_pass": True,
                },
                "style_policy": {
                    "formatters": [["ruff", "format", "."]],
                    "run_before_commit": True,
                },
            },
        },
    }


def _config_without_reviewer():
    """Конфиг без reviewer_id — прямой done."""
    return {
        "supervisor": {"confidence_threshold": 70},
        "workers": {
            "job1_worker": {
                "max_attempts": 1,
            },
        },
    }


def _reviewer_approved_json():
    """Reviewer response: APPROVED."""
    return """\
<<<JSON>>>
{
  "verdict": "APPROVED",
  "feedback": "Code looks good",
  "issues": []
}
<<<END>>>
"""


def _reviewer_needs_changes_json():
    """Reviewer response: NEEDS_CHANGES."""
    return """\
<<<JSON>>>
{
  "verdict": "NEEDS_CHANGES",
  "feedback": "Fix the bug",
  "issues": [
    {"repo": "api", "file": "app.py", "line": 42, "type": "bug", "message": "Missing null check"}
  ]
}
<<<END>>>
"""


# ── Тесты ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reviewer_approved_triggers_push(
    db_path, mock_tg_handler, mock_repo_manager
):
    """worker done -> reviewer APPROVED -> CI pass -> push -> done."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_reviewer()

    # Worker returns done, reviewer returns APPROVED
    call_count = 0

    async def mock_run_claude(prompt, cwd=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Worker call
            return WORKER_DONE_JSON
        else:
            # Reviewer call
            return _reviewer_approved_json()

    with (
        patch(
            "supervisor.claude_runner.run_claude",
            AsyncMock(side_effect=mock_run_claude),
        ),
        patch(
            "supervisor.stages.deliver.safe_exec", return_value=("", "", 0)
        ) as mock_safe_exec,
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    # Task should be done
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert row["status"] == "done"

    # TG notification should mention APPROVED
    mock_tg_handler.notify_owner.assert_called()
    last_msg = mock_tg_handler.notify_owner.call_args[0][0]
    assert "APPROVED" in last_msg or "DONE" in last_msg

    # safe_exec should have been called (CI/push commands)
    assert mock_safe_exec.call_count > 0


@pytest.mark.asyncio
async def test_reviewer_needs_changes_retries(
    db_path, mock_tg_handler, mock_repo_manager
):
    """NEEDS_CHANGES -> worker retry -> APPROVED -> done."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_reviewer()

    call_count = 0

    async def mock_run_claude(prompt, cwd=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Initial worker call
            return WORKER_DONE_JSON
        elif call_count == 2:
            # First reviewer -> NEEDS_CHANGES
            return _reviewer_needs_changes_json()
        elif call_count == 3:
            # Worker retry
            return WORKER_DONE_JSON
        else:
            # Second reviewer -> APPROVED
            return _reviewer_approved_json()

    with (
        patch(
            "supervisor.claude_runner.run_claude",
            AsyncMock(side_effect=mock_run_claude),
        ),
        patch("supervisor.stages.deliver.safe_exec", return_value=("", "", 0)),
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    # Task should be done after retry
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert row["status"] == "done"

    # 4 calls: worker + reviewer(NEEDS) + worker_retry + reviewer(APPROVED)
    assert call_count == 4


@pytest.mark.asyncio
async def test_reviewer_exhausted_iterations(
    db_path, mock_tg_handler, mock_repo_manager
):
    """3x NEEDS_CHANGES -> requires_manual."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_reviewer()
    # max_review_iterations = 3

    call_count = 0

    async def mock_run_claude(prompt, cwd=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Initial worker
            return WORKER_DONE_JSON
        elif call_count % 2 == 0:
            # Reviewers always NEEDS_CHANGES
            return _reviewer_needs_changes_json()
        else:
            # Worker retries
            return WORKER_DONE_JSON

    with (
        patch(
            "supervisor.claude_runner.run_claude",
            AsyncMock(side_effect=mock_run_claude),
        ),
        patch("supervisor.stages.deliver.safe_exec", return_value=("", "", 0)),
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    # Task should be requires_manual
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert row["status"] == "requires_manual"

    # TG notification mentions exhausted/retry
    mock_tg_handler.notify_owner.assert_called()
    last_msg = mock_tg_handler.notify_owner.call_args[0][0]
    assert "exhausted" in last_msg or "/retry" in last_msg


@pytest.mark.asyncio
async def test_no_reviewer_skips_review(db_path, mock_tg_handler, mock_repo_manager):
    """No reviewer_id in config -> straight to done (current behaviour)."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_without_reviewer()

    with patch(
        "supervisor.claude_runner.run_claude", AsyncMock(return_value=WORKER_DONE_JSON)
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    # Task done without review
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert row["status"] == "done"

    # Notification should have DONE
    mock_tg_handler.notify_owner.assert_called_once()
    msg = mock_tg_handler.notify_owner.call_args[0][0]
    assert "DONE" in msg


@pytest.mark.asyncio
async def test_ci_failure_blocks_push(db_path, mock_tg_handler, mock_repo_manager):
    """Reviewer APPROVED but CI fails -> requires_manual."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_reviewer()

    call_count = 0

    async def mock_run_claude(prompt, cwd=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return WORKER_DONE_JSON
        else:
            return _reviewer_approved_json()

    safe_exec_call_count = 0

    def mock_safe_exec(cmd, cwd, timeout=300, **kwargs):
        nonlocal safe_exec_call_count
        safe_exec_call_count += 1
        # Style formatters + git add succeed
        if cmd[0] == "ruff" and cmd[1] == "format":
            return ("", "", 0)
        if cmd[0] == "git" and cmd[1] == "add":
            return ("", "", 0)
        if cmd[0] == "git" and cmd[1] == "commit":
            return ("", "", 0)
        # CI pytest fails
        if cmd[0] == "pytest":
            return ("", "FAILED tests", 1)
        # Everything else
        return ("", "", 0)

    with (
        patch(
            "supervisor.claude_runner.run_claude",
            AsyncMock(side_effect=mock_run_claude),
        ),
        patch("supervisor.stages.deliver.safe_exec", side_effect=mock_safe_exec),
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    # Task should be requires_manual because CI failed
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert row["status"] == "requires_manual"

    # Error reason should be git_push_failed
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT last_error_reason FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert row["last_error_reason"] == "git_push_failed"


@pytest.mark.asyncio
async def test_reviewer_invalid_json(db_path, mock_tg_handler, mock_repo_manager):
    """Reviewer outputs invalid JSON -> requires_manual."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_reviewer()

    call_count = 0

    async def mock_run_claude(prompt, cwd=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return WORKER_DONE_JSON
        else:
            # Reviewer returns garbage
            return "I don't know what to do here, sorry."

    with (
        patch(
            "supervisor.claude_runner.run_claude",
            AsyncMock(side_effect=mock_run_claude),
        ),
        patch("supervisor.stages.deliver.safe_exec", return_value=("", "", 0)),
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    # Task should be requires_manual
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert row["status"] == "requires_manual"

    # TG notification about invalid JSON
    mock_tg_handler.notify_owner.assert_called()


# ── Тесты для prompt builders ─────────────────────────────────────────────────


def test_build_reviewer_prompt_includes_task():
    """_build_reviewer_prompt includes task description and worker notes."""
    from supervisor.main import _build_reviewer_prompt

    prompt = _build_reviewer_prompt(
        task_description="Implement feature X",
        worker_result={
            "notes": "Done implementing",
            "repos": [{"alias": "api", "changed_files": ["app.py", "test.py"]}],
        },
        repos_context=[{"alias": "api", "path": "workspace/123/api"}],
    )

    assert "Implement feature X" in prompt
    assert "Done implementing" in prompt
    assert "app.py" in prompt
    assert "workspace/123/api" in prompt
    assert "<<<JSON>>>" in prompt
    assert "APPROVED" in prompt


def test_build_worker_retry_prompt_includes_issues():
    """_build_worker_retry_prompt includes reviewer issues."""
    from supervisor.main import _build_worker_retry_prompt

    prompt = _build_worker_retry_prompt(
        task_description="Fix bug Y",
        attempt=2,
        max_attempts=3,
        prev_notes="First attempt done",
        reviewer_issues=[
            {
                "repo": "api",
                "file": "app.py",
                "line": 42,
                "type": "bug",
                "message": "Missing null check",
            }
        ],
        repos_context=[{"alias": "api", "path": "workspace/123/api"}],
        branch="ai/task-123",
    )

    assert "Fix bug Y" in prompt
    assert "2/3" in prompt
    assert "First attempt done" in prompt
    assert "Missing null check" in prompt
    assert "app.py:42" in prompt
    assert "<<<JSON>>>" in prompt


# ── Тесты для _run_ci_and_push ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ci_and_push_success(mock_repo_manager):
    """_run_ci_and_push succeeds when all commands pass."""
    from supervisor.main import _run_ci_and_push

    worker_cfg = {
        "style_policy": {
            "formatters": [["ruff", "format", "."]],
            "run_before_commit": True,
        },
        "ci_policy": {
            "run_before_push": [["pytest"]],
            "required_pass": True,
        },
    }

    with patch("supervisor.stages.deliver.safe_exec", return_value=("", "", 0)):
        success, err = await _run_ci_and_push(
            task_id="abc123",
            job="job1",
            alias="api",
            worker_cfg=worker_cfg,
            repo_mgr=mock_repo_manager,
        )

    assert success is True
    assert err == ""


@pytest.mark.asyncio
async def test_reviewer_gets_agent_symlink(
    db_path, mock_tg_handler, mock_repo_manager
):
    """BUG-005: reviewer stage creates symlink so reviewer can see worktree files."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_reviewer()

    call_count = 0

    async def mock_run_claude(prompt, cwd=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return WORKER_DONE_JSON
        else:
            return _reviewer_approved_json()

    with (
        patch(
            "supervisor.claude_runner.run_claude",
            AsyncMock(side_effect=mock_run_claude),
        ),
        patch("supervisor.stages.deliver.safe_exec", return_value=("", "", 0)),
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    # ensure_agent_symlink should have been called for reviewer
    mock_repo_manager.ensure_agent_symlink.assert_called_once_with(
        task["id"], "job1_reviewer"
    )
    # cleanup should clean reviewer symlink too
    mock_repo_manager.cleanup_agent_symlink.assert_called_once_with(
        task["id"], "job1_reviewer"
    )


@pytest.mark.asyncio
async def test_ci_and_push_ci_fails(mock_repo_manager):
    """_run_ci_and_push fails when CI check fails."""
    from supervisor.main import _run_ci_and_push

    worker_cfg = {
        "style_policy": {"formatters": [], "run_before_commit": False},
        "ci_policy": {
            "run_before_push": [["pytest"]],
            "required_pass": True,
        },
    }

    call_count = 0

    def mock_safe_exec(cmd, cwd, timeout=300, **kwargs):
        nonlocal call_count
        call_count += 1
        if cmd[0] == "git":
            return ("", "", 0)
        # pytest fails
        return ("", "test failures", 1)

    with patch("supervisor.stages.deliver.safe_exec", side_effect=mock_safe_exec):
        success, err = await _run_ci_and_push(
            task_id="abc123",
            job="job1",
            alias="api",
            worker_cfg=worker_cfg,
            repo_mgr=mock_repo_manager,
        )

    assert success is False
    assert "CI failed" in err
