"""
tests/test_cost_and_session.py — Tests for cost_tracker, session_manager, and run_logger metrics.
"""

import asyncio
import os

import pytest

from storage.db import create_task, get_conn, init_db
from storage.migrate import apply_migrations
from supervisor.cost_tracker import EMPTY_METRICS, extract_metrics, run_claude_tracked
from supervisor.session_manager import (
    build_resumed_prompt,
    extract_progress_summary,
    load_checkpoint,
    mark_resumed,
    save_checkpoint,
    should_refresh_session,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    """Temporary DB with full schema and migrations. Sets DB_PATH env."""
    path = str(tmp_path / "test.db")
    os.environ["DB_PATH"] = path
    init_db(path)
    with get_conn(path) as conn:
        apply_migrations(conn)
    yield path
    os.environ.pop("DB_PATH", None)


# ── cost_tracker tests ─────────────────────────────────────────────────────────


class TestExtractMetrics:
    """Tests for cost_tracker.extract_metrics."""

    def test_extract_metrics_full(self):
        """Full Claude CLI JSON response -> all metrics extracted correctly."""
        response = {
            "result": "hello",
            "duration_ms": 5000,
            "duration_api_ms": 3000,
            "total_cost_usd": 0.085,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 12000,
                "cache_read_input_tokens": 5000,
            },
            "modelUsage": {
                "claude-opus-4-6[1m]": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "costUSD": 0.085,
                    "contextWindow": 1000000,
                    "maxOutputTokens": 64000,
                }
            },
        }
        metrics = extract_metrics(response)
        assert metrics["elapsed_ms"] == 5000
        assert metrics["input_tokens"] == 100
        assert metrics["output_tokens"] == 50
        assert metrics["cache_creation_tokens"] == 12000
        assert metrics["cost_usd"] == 0.085
        assert metrics["model_id"] == "claude-opus-4-6[1m]"
        assert metrics["context_window"] == 1000000

    def test_extract_metrics_empty(self):
        """Empty response -> all zeros/defaults."""
        metrics = extract_metrics({})
        assert metrics["elapsed_ms"] == 0
        assert metrics["input_tokens"] == 0
        assert metrics["output_tokens"] == 0
        assert metrics["cache_creation_tokens"] == 0
        assert metrics["cost_usd"] == 0.0
        assert metrics["model_id"] == ""
        assert metrics["context_window"] == 0
        assert metrics["max_output_tokens"] == 0


class TestRunClaudeTracked:
    """Tests for cost_tracker.run_claude_tracked with DI runner."""

    async def test_run_claude_tracked_with_di_runner(self):
        """DI runner returns text -> (result, metrics) tuple with elapsed_ms > 0."""

        async def mock_runner(prompt, cwd=None, timeout=300):
            return "hello"

        result, metrics = await run_claude_tracked(
            prompt="test prompt", runner=mock_runner
        )
        assert result == "hello"
        assert metrics["elapsed_ms"] >= 0
        # Verify metrics structure matches EMPTY_METRICS keys
        for key in EMPTY_METRICS:
            assert key in metrics

    async def test_run_claude_tracked_runner_measures_time(self):
        """Runner sleeps 50ms -> elapsed_ms >= 50."""

        async def slow_runner(prompt, cwd=None, timeout=300):
            await asyncio.sleep(0.05)
            return "done"

        result, metrics = await run_claude_tracked(
            prompt="test prompt", runner=slow_runner
        )
        assert result == "done"
        assert metrics["elapsed_ms"] >= 50


# ── session_manager tests ──────────────────────────────────────────────────────


    def test_extract_metrics_partial_no_usage(self):
        """Response with duration but no usage -> tokens default to 0."""
        m = extract_metrics({"duration_ms": 1500, "total_cost_usd": 0.01})
        assert m["elapsed_ms"] == 1500
        assert m["cost_usd"] == 0.01
        assert m["input_tokens"] == 0
        assert m["model_id"] == ""

    def test_extract_metrics_multiple_models_picks_first(self):
        """modelUsage with 2 models -> picks first key."""
        m = extract_metrics({
            "modelUsage": {
                "model-a": {"contextWindow": 200000, "maxOutputTokens": 8000},
                "model-b": {"contextWindow": 100000, "maxOutputTokens": 4000},
            }
        })
        assert m["model_id"] == "model-a"
        assert m["context_window"] == 200000


class TestRunClaudeTrackedDI:
    """Edge case tests for run_claude_tracked with DI runner."""

    @pytest.mark.asyncio
    async def test_di_runner_forwards_cwd_timeout(self):
        """Verify cwd and timeout are forwarded to injected runner."""
        captured = {}

        async def spy(prompt, cwd=None, timeout=300):
            captured["cwd"] = cwd
            captured["timeout"] = timeout
            return "ok"

        await run_claude_tracked(prompt="p", cwd="/tmp/test", timeout=60, runner=spy)
        assert captured["cwd"] == "/tmp/test"
        assert captured["timeout"] == 60


class TestSessionCheckpoints:
    """Tests for session_manager checkpoint save/load/resume."""

    def _create_test_task(self, db_path: str) -> str:
        """Helper to create a task in the test DB."""
        return create_task(
            source="test",
            source_contact="test",
            assigned_worker="w1",
            description="test task",
            client_contact="test",
        )

    def test_save_and_load_checkpoint(self, db_path):
        """Save checkpoint then load it -> fields match."""
        task_id = self._create_test_task(db_path)
        metrics = {
            "input_tokens": 5000,
            "cache_creation_tokens": 1000,
            "output_tokens": 2000,
            "cost_usd": 0.05,
        }
        checkpoint_id = save_checkpoint(
            task_id=task_id,
            worker_id="w1",
            phase="worker",
            attempt=1,
            metrics=metrics,
            progress_summary="did some work",
            saved_context="context prefix",
            db_path=db_path,
        )
        assert checkpoint_id > 0

        loaded = load_checkpoint(task_id, db_path=db_path)
        assert loaded is not None
        assert loaded["task_id"] == task_id
        assert loaded["worker_id"] == "w1"
        assert loaded["phase"] == "worker"
        assert loaded["attempt"] == 1
        assert loaded["input_tokens"] == 6000  # 5000 + 1000 cache_creation
        assert loaded["output_tokens"] == 2000
        assert loaded["cost_usd"] == 0.05
        assert loaded["progress_summary"] == "did some work"
        assert loaded["saved_context"] == "context prefix"
        assert loaded["resumed"] == 0

    def test_load_checkpoint_none(self, db_path):
        """No checkpoint saved -> returns None."""
        task_id = self._create_test_task(db_path)
        loaded = load_checkpoint(task_id, db_path=db_path)
        assert loaded is None

    def test_mark_resumed(self, db_path):
        """Save, mark resumed, load -> returns None (already resumed)."""
        task_id = self._create_test_task(db_path)
        metrics = {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01}
        checkpoint_id = save_checkpoint(
            task_id=task_id,
            worker_id="w1",
            phase="worker",
            attempt=1,
            metrics=metrics,
            progress_summary="progress",
            db_path=db_path,
        )

        mark_resumed(checkpoint_id, db_path=db_path)

        loaded = load_checkpoint(task_id, db_path=db_path)
        assert loaded is None


class TestShouldRefreshSession:
    """Tests for session_manager.should_refresh_session."""

    def test_should_refresh_context_limit(self):
        """850k input tokens out of 1M context -> True (over 80% threshold)."""
        metrics = {
            "input_tokens": 850000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 1000,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        assert should_refresh_session(metrics) is True

    def test_should_refresh_below_threshold(self):
        """500k tokens out of 1M -> False (under 80% threshold)."""
        metrics = {
            "input_tokens": 500000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 1000,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        assert should_refresh_session(metrics) is False

    def test_should_refresh_output_limit(self):
        """output_tokens=60000 out of max 64000 -> True (over 90% threshold)."""
        metrics = {
            "input_tokens": 100000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 60000,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        assert should_refresh_session(metrics) is True

    def test_should_refresh_no_context_info(self):
        """context_window=0 -> False (can't determine)."""
        metrics = {
            "input_tokens": 900000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 1000,
            "context_window": 0,
            "max_output_tokens": 0,
        }
        assert should_refresh_session(metrics) is False


    def test_should_refresh_exactly_at_threshold_is_false(self):
        """Exactly at 80% (800k/1M) -> False (strict > comparison)."""
        metrics = {
            "input_tokens": 800000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 1000,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        assert should_refresh_session(metrics) is False

    def test_should_refresh_cache_tokens_count(self):
        """cache_creation + cache_read push total over threshold."""
        metrics = {
            "input_tokens": 300000,
            "cache_creation_tokens": 300000,
            "cache_read_tokens": 250000,
            "output_tokens": 1000,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        # 300k + 300k + 250k = 850k > 800k
        assert should_refresh_session(metrics) is True

    def test_should_refresh_custom_threshold(self):
        """Custom context_threshold=0.5 triggers at 600k/1M."""
        metrics = {
            "input_tokens": 600000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 1000,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        assert should_refresh_session(metrics, context_threshold=0.5) is True
        assert should_refresh_session(metrics, context_threshold=0.8) is False


class TestBuildResumedPrompt:
    """Tests for session_manager.build_resumed_prompt."""

    def test_build_resumed_prompt(self):
        """Verify contains [SESSION RESUMED], original description, and progress."""
        checkpoint = {
            "progress_summary": "implemented function X",
            "input_tokens": 5000,
            "output_tokens": 2000,
            "attempt": 1,
        }
        prompt = build_resumed_prompt(
            original_description="Create a utility module",
            checkpoint=checkpoint,
            plan_text="Step 1: do X. Step 2: do Y.",
        )
        assert "[SESSION RESUMED" in prompt
        assert "Create a utility module" in prompt
        assert "implemented function X" in prompt
        assert "tokens used: 7000" in prompt
        assert "attempt 2" in prompt
        assert "Step 1: do X" in prompt


class TestExtractProgress:
    """Tests for session_manager.extract_progress_summary."""

    def test_extract_progress_from_json(self):
        """stdout with <<<JSON>>>{...notes: 'did stuff'}<<<END>>> -> extracts 'did stuff'."""
        stdout = 'Some log output\n<<<JSON>>>{"status":"done","confidence":90,"result":{"repos":[],"notes":"did stuff"},"question":null}<<<END>>>\n'
        progress = extract_progress_summary(stdout)
        assert "did stuff" in progress

    def test_extract_progress_fallback(self):
        """Plain text without JSON markers -> last 500 chars."""
        text = "A" * 1000
        progress = extract_progress_summary(text)
        assert len(progress) == 500
        assert progress == "A" * 500


# ── run_logger metrics tests ──────────────────────────────────────────────────


class TestLogRunWithMetrics:
    """Tests for run_logger.log_run with metrics dict."""

    def test_log_run_with_metrics(self, db_path, tmp_path):
        """Call log_run with metrics dict -> verify new columns in DB."""
        from supervisor.run_logger import log_run

        task_id = create_task(
            source="test",
            source_contact="test",
            assigned_worker="w1",
            description="test",
            client_contact="test",
        )

        metrics = {
            "elapsed_ms": 5000,
            "api_elapsed_ms": 3000,
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_tokens": 12000,
            "cache_read_tokens": 5000,
            "cost_usd": 0.085,
            "model_id": "claude-opus-4-6[1m]",
        }

        stdout_path, stderr_path = log_run(
            task_id=task_id,
            phase="worker",
            stdout="hello world",
            stderr="",
            parsed_json={"status": "done"},
            json_valid=True,
            returncode=0,
            attempt=1,
            db_path=db_path,
            logs_dir=str(tmp_path / "logs"),
            worker_id="w1",
            metrics=metrics,
        )

        # Verify files exist
        assert os.path.exists(stdout_path)
        assert os.path.exists(stderr_path)

        # Verify DB record with metric columns
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()

        assert row is not None
        assert row["elapsed_ms"] == 5000
        assert row["api_elapsed_ms"] == 3000
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["cache_creation_tokens"] == 12000
        assert row["cache_read_tokens"] == 5000
        assert abs(row["cost_usd"] - 0.085) < 1e-6
        assert row["model_id"] == "claude-opus-4-6[1m]"
