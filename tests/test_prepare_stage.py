"""
tests/test_prepare_stage.py — Tests for supervisor/stages/prepare.py.

Covers:
- prepare_stage success (token set, worktrees created)
- prepare_stage lease conflict (acquire_lease returns None -> LeaseConflict)
- prepare_stage with repos (mirror + worktree setup called)
- prepare_stage without repos (worktree setup skipped)
- prepare_stage mirror failure -> WorktreeError
- _setup_worktrees branch substitution
- _setup_worktrees multiple repos
- _setup_worktrees token_env resolution

Запуск: pytest tests/test_prepare_stage.py -v
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from supervisor.pipeline import LeaseConflict, WorkerContext, WorktreeError
from supervisor.stages.prepare import _setup_worktrees, prepare_stage


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_ctx(**overrides) -> WorkerContext:
    """Create a WorkerContext with test defaults."""
    from unittest.mock import AsyncMock

    defaults = dict(
        task_id="task-abc123",
        worker_id="job1_worker",
        task_description="test task",
        config={"supervisor": {}, "workers": {"job1_worker": {}}},
        tg_handler=AsyncMock(),
        db_path=None,
        job="job1",
        worker_dir="workers/job1_worker",
        repos=[],
        branch_pattern="ai/task-{task_id}",
        base_branch="main",
        lease_ttl=600,
    )
    defaults.update(overrides)
    return WorkerContext(**defaults)


# ── prepare_stage tests ──────────────────────────────────────────────────────


class TestPrepareStage:

    @pytest.mark.asyncio
    async def test_success_sets_token(self):
        """prepare_stage acquires lease and sets ctx.token."""
        ctx = _make_ctx()
        with patch(
            "supervisor.stages.prepare.acquire_lease",
            return_value="lease-token-xyz",
        ):
            await prepare_stage(ctx)
        assert ctx.token == "lease-token-xyz"

    @pytest.mark.asyncio
    async def test_lease_conflict_raises(self):
        """prepare_stage raises LeaseConflict when acquire_lease returns None."""
        ctx = _make_ctx()
        with patch(
            "supervisor.stages.prepare.acquire_lease",
            return_value=None,
        ):
            with pytest.raises(LeaseConflict):
                await prepare_stage(ctx)

    @pytest.mark.asyncio
    async def test_with_repos_creates_worktrees(self):
        """prepare_stage with repos calls ensure_mirror + prepare_worktree."""
        mock_repo_mgr = MagicMock()
        ctx = _make_ctx(
            repos=[
                {"alias": "api", "url": "https://github.com/org/api.git"},
            ],
            repo_manager=mock_repo_mgr,
        )
        with patch(
            "supervisor.stages.prepare.acquire_lease",
            return_value="tok-1",
        ):
            await prepare_stage(ctx)

        assert ctx.token == "tok-1"
        mock_repo_mgr.ensure_mirror.assert_called_once()
        mock_repo_mgr.prepare_worktree.assert_called_once()
        assert ctx.worktree_aliases == ["api"]
        assert ctx.branch == "ai/task-task-abc123"
        assert len(ctx.repos_context) == 1
        assert ctx.repos_context[0]["alias"] == "api"

    @pytest.mark.asyncio
    async def test_without_repos_skips_worktree_setup(self):
        """prepare_stage without repos skips worktree entirely."""
        ctx = _make_ctx(repos=[])
        with patch(
            "supervisor.stages.prepare.acquire_lease",
            return_value="tok-2",
        ):
            await prepare_stage(ctx)

        assert ctx.token == "tok-2"
        assert ctx.worktree_aliases == []
        assert ctx.repos_context is None  # never set

    @pytest.mark.asyncio
    async def test_mirror_failure_raises_worktree_error(self):
        """prepare_stage raises WorktreeError when ensure_mirror fails."""
        mock_repo_mgr = MagicMock()
        mock_repo_mgr.ensure_mirror.side_effect = RuntimeError("clone failed")
        ctx = _make_ctx(
            repos=[{"alias": "api", "url": "https://github.com/org/api.git"}],
            repo_manager=mock_repo_mgr,
        )
        with patch(
            "supervisor.stages.prepare.acquire_lease",
            return_value="tok-3",
        ):
            with pytest.raises(WorktreeError) as exc_info:
                await prepare_stage(ctx)
            assert "clone failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_prepare_worktree_failure_raises_worktree_error(self):
        """prepare_stage raises WorktreeError when prepare_worktree fails."""
        mock_repo_mgr = MagicMock()
        mock_repo_mgr.ensure_mirror.return_value = None  # success
        mock_repo_mgr.prepare_worktree.side_effect = RuntimeError("branch conflict")
        ctx = _make_ctx(
            repos=[{"alias": "api", "url": "https://github.com/org/api.git"}],
            repo_manager=mock_repo_mgr,
        )
        with patch(
            "supervisor.stages.prepare.acquire_lease",
            return_value="tok-4",
        ):
            with pytest.raises(WorktreeError) as exc_info:
                await prepare_stage(ctx)
            assert "branch conflict" in str(exc_info.value)


# ── _setup_worktrees tests ───────────────────────────────────────────────────


class TestSetupWorktrees:

    def test_branch_substitution(self):
        """_setup_worktrees replaces {task_id} in branch_pattern."""
        mock_repo_mgr = MagicMock()
        repos = [{"alias": "api", "url": "https://github.com/org/api.git"}]

        aliases = _setup_worktrees(
            task_id="task-xyz",
            job="job1",
            repos=repos,
            branch_pattern="ai/task-{task_id}",
            base_branch="main",
            repo_mgr=mock_repo_mgr,
        )

        assert aliases == ["api"]
        # Verify prepare_worktree was called with substituted branch
        mock_repo_mgr.prepare_worktree.assert_called_once_with(
            "task-xyz", "job1", "api", "ai/task-task-xyz", "main"
        )

    def test_multiple_repos(self):
        """_setup_worktrees creates worktrees for each repo."""
        mock_repo_mgr = MagicMock()
        repos = [
            {"alias": "api", "url": "https://github.com/org/api.git"},
            {"alias": "frontend", "url": "https://github.com/org/frontend.git"},
            {"alias": "docs", "url": "https://github.com/org/docs.git"},
        ]

        aliases = _setup_worktrees(
            task_id="task-multi",
            job="job1",
            repos=repos,
            branch_pattern="ai/{task_id}",
            base_branch="develop",
            repo_mgr=mock_repo_mgr,
        )

        assert aliases == ["api", "frontend", "docs"]
        assert mock_repo_mgr.ensure_mirror.call_count == 3
        assert mock_repo_mgr.prepare_worktree.call_count == 3

    def test_token_env_resolved(self):
        """_setup_worktrees resolves token_env from environment variable."""
        mock_repo_mgr = MagicMock()
        repos = [
            {
                "alias": "private",
                "url": "https://github.com/org/private.git",
                "token_env": "GH_TOKEN_TEST",
            }
        ]

        os.environ["GH_TOKEN_TEST"] = "ghp_secret123"
        try:
            _setup_worktrees(
                task_id="task-tok",
                job="job1",
                repos=repos,
                branch_pattern="ai/{task_id}",
                base_branch="main",
                repo_mgr=mock_repo_mgr,
            )

            # ensure_mirror called with token from env
            call_kwargs = mock_repo_mgr.ensure_mirror.call_args
            assert call_kwargs[1].get("token") == "ghp_secret123" or (
                len(call_kwargs[0]) >= 5 and call_kwargs[0][4] == "ghp_secret123"
            ) or "ghp_secret123" in str(call_kwargs)
        finally:
            del os.environ["GH_TOKEN_TEST"]

    def test_clone_strategy_passed(self):
        """_setup_worktrees passes clone_strategy from repo config."""
        mock_repo_mgr = MagicMock()
        repos = [
            {
                "alias": "api",
                "url": "https://github.com/org/api.git",
                "clone_strategy": "shallow",
            }
        ]

        _setup_worktrees(
            task_id="task-strat",
            job="job1",
            repos=repos,
            branch_pattern="ai/{task_id}",
            base_branch="main",
            repo_mgr=mock_repo_mgr,
        )

        call_kwargs = mock_repo_mgr.ensure_mirror.call_args
        # clone_strategy is passed as keyword arg
        assert call_kwargs[1].get("clone_strategy") == "shallow" or (
            "shallow" in str(call_kwargs)
        )
