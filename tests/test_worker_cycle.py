"""
tests/test_worker_cycle.py — тесты для worktree-интеграции в run_worker_cycle.

Запуск: pytest tests/test_worker_cycle.py -v

Тестируем:
- Worktree setup/cleanup в цикле воркера
- Ошибки при создании worktree
- Backward compatibility (без repos — worktree не создаётся)
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from storage.db import get_conn, create_task
from tests.conftest import WORKER_DONE_JSON


# ── Фикстуры ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_repo_manager():
    """Мок RepoManager с трекингом вызовов."""
    mgr = MagicMock()
    mgr.ensure_mirror = MagicMock(return_value=Path("/fake/mirror"))
    mgr.prepare_worktree = MagicMock(return_value=Path("/fake/worktree"))
    mgr.cleanup_worktree = MagicMock()
    return mgr


def _make_task(db_path, worker_id="job1_worker"):
    """Создать задачу в DB и вернуть dict."""
    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker=worker_id,
        description="test task",
        client_contact="42",
    )
    return {"id": task_id, "assigned_worker": worker_id, "description": "test task"}


def _config_with_repos():
    """Конфиг с repos для worktree тестов."""
    return {
        "supervisor": {"confidence_threshold": 70},
        "workers": {
            "job1_worker": {
                "max_attempts": 1,
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
            }
        },
    }


# ── Тесты: worktree setup ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worktree_setup_called(db_path, mock_tg_handler, mock_repo_manager):
    """При наличии repos — вызываются ensure_mirror и prepare_worktree."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_repos()

    with patch(
        "supervisor.claude_runner.run_claude", AsyncMock(return_value=WORKER_DONE_JSON)
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    mock_repo_manager.ensure_mirror.assert_called_once_with(
        "job1",
        "api",
        "https://github.com/org/api",
        clone_strategy="shallow",
        token=None,  # GIT_TOKEN_JOB1 не установлен в env
    )
    mock_repo_manager.prepare_worktree.assert_called_once()
    wt_call = mock_repo_manager.prepare_worktree.call_args
    assert wt_call[0][0] == task["id"]  # task_id
    assert wt_call[0][1] == "job1"  # job
    assert wt_call[0][2] == "api"  # alias


@pytest.mark.asyncio
async def test_worktree_cleanup_on_done(db_path, mock_tg_handler, mock_repo_manager):
    """После done — worktree cleanup вызывается."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_repos()

    with patch(
        "supervisor.claude_runner.run_claude", AsyncMock(return_value=WORKER_DONE_JSON)
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    mock_repo_manager.cleanup_worktree.assert_called_once_with(
        task["id"],
        "job1",
        "api",
    )


@pytest.mark.asyncio
async def test_worktree_cleanup_on_error(db_path, mock_tg_handler, mock_repo_manager):
    """После ошибки claude — worktree cleanup всё равно вызывается."""
    from supervisor.main import run_worker_cycle
    from supervisor.claude_runner import ClaudeRunnerError

    task = _make_task(db_path)
    config = _config_with_repos()

    with patch(
        "supervisor.claude_runner.run_claude",
        AsyncMock(side_effect=ClaudeRunnerError("crash")),
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    mock_repo_manager.cleanup_worktree.assert_called_once_with(
        task["id"],
        "job1",
        "api",
    )


@pytest.mark.asyncio
async def test_worktree_setup_failure(db_path, mock_tg_handler, mock_repo_manager):
    """Если worktree setup падает — task → requires_manual, TG уведомление."""
    from supervisor.main import run_worker_cycle
    from supervisor.repo_manager import RepoManagerError

    task = _make_task(db_path)
    config = _config_with_repos()

    mock_repo_manager.ensure_mirror.side_effect = RepoManagerError("git clone failed")

    await run_worker_cycle(task, config, mock_tg_handler, db_path, mock_repo_manager)

    # Task перешёл в requires_manual
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert row["status"] == "requires_manual"

    # TG уведомление отправлено
    mock_tg_handler.notify_owner.assert_called_once()
    msg = mock_tg_handler.notify_owner.call_args[0][0]
    assert "worktree" in msg.lower()


@pytest.mark.asyncio
async def test_no_repos_no_worktree(db_path, mock_tg_handler, mock_repo_manager):
    """Без repos в конфиге — worktree функции не вызываются."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = {"supervisor": {}, "workers": {"job1_worker": {"max_attempts": 1}}}

    with patch(
        "supervisor.claude_runner.run_claude", AsyncMock(return_value=WORKER_DONE_JSON)
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    mock_repo_manager.ensure_mirror.assert_not_called()
    mock_repo_manager.prepare_worktree.assert_not_called()
    # cleanup тоже не вызывается (worktree_aliases пустой)
    mock_repo_manager.cleanup_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_worktree_branch_pattern(db_path, mock_tg_handler, mock_repo_manager):
    """Branch pattern подставляет task_id."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_repos()

    with patch(
        "supervisor.claude_runner.run_claude", AsyncMock(return_value=WORKER_DONE_JSON)
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    wt_call = mock_repo_manager.prepare_worktree.call_args
    branch = wt_call[0][3]  # 4th positional arg = branch
    assert task["id"] in branch
    assert branch.startswith("ai/task-")


@pytest.mark.asyncio
async def test_worktree_cleanup_on_unexpected_exception(
    db_path, mock_tg_handler, mock_repo_manager
):
    """Даже при неожиданной ошибке — cleanup worktree срабатывает."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = _config_with_repos()

    # run_claude возвращает строку, но extract_json вернёт что-то неожиданное
    # Симулируем неожиданную ошибку через side_effect на run_claude
    async def _explode(*a, **kw):
        raise RuntimeError("unexpected internal error")

    with patch("supervisor.claude_runner.run_claude", AsyncMock(side_effect=_explode)):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    # Cleanup должен быть вызван через finally
    mock_repo_manager.cleanup_worktree.assert_called_once()


@pytest.mark.asyncio
async def test_multiple_repos_all_cleaned(db_path, mock_tg_handler, mock_repo_manager):
    """При нескольких repos — все worktrees создаются и все cleanup'ятся."""
    from supervisor.main import run_worker_cycle

    task = _make_task(db_path)
    config = {
        "supervisor": {"confidence_threshold": 70},
        "workers": {
            "job1_worker": {
                "max_attempts": 1,
                "repos": [
                    {
                        "alias": "api",
                        "url": "https://github.com/org/api",
                        "clone_strategy": "mirror",
                    },
                    {
                        "alias": "frontend",
                        "url": "https://github.com/org/frontend",
                        "clone_strategy": "shallow",
                    },
                ],
                "branching_policy": {"pattern": "ai/task-{task_id}", "base": "main"},
            }
        },
    }

    with patch(
        "supervisor.claude_runner.run_claude", AsyncMock(return_value=WORKER_DONE_JSON)
    ):
        await run_worker_cycle(
            task, config, mock_tg_handler, db_path, mock_repo_manager
        )

    assert mock_repo_manager.ensure_mirror.call_count == 2
    assert mock_repo_manager.prepare_worktree.call_count == 2
    assert mock_repo_manager.cleanup_worktree.call_count == 2

    cleanup_aliases = [
        c[0][2] for c in mock_repo_manager.cleanup_worktree.call_args_list
    ]
    assert "api" in cleanup_aliases
    assert "frontend" in cleanup_aliases
