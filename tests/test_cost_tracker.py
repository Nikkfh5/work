"""
tests/test_cost_tracker.py — Tests for supervisor/cost_tracker.py.

Covers:
- extract_metrics with missing usage key
- extract_metrics with partial data (only some fields)
- run_claude_tracked DI mode (runner kwarg)
- run_claude_tracked DI mode exception propagation
- run_claude_tracked real mode with malformed JSON (mock subprocess)
- run_claude_tracked real mode with valid JSON response

Запуск: pytest tests/test_cost_tracker.py -v
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supervisor.cost_tracker import EMPTY_METRICS, extract_metrics, run_claude_tracked


# ── extract_metrics tests ────────────────────────────────────────────────────


class TestExtractMetrics:

    def test_missing_usage_key(self):
        """Response without 'usage' key -> tokens default to 0."""
        response = {
            "result": "hello",
            "duration_ms": 2000,
            "total_cost_usd": 0.01,
            "modelUsage": {"model-a": {"contextWindow": 200000}},
        }
        m = extract_metrics(response)
        assert m["input_tokens"] == 0
        assert m["output_tokens"] == 0
        assert m["cache_creation_tokens"] == 0
        assert m["cache_read_tokens"] == 0
        assert m["elapsed_ms"] == 2000
        assert m["cost_usd"] == 0.01
        assert m["model_id"] == "model-a"

    def test_partial_data_only_duration(self):
        """Response with only duration_ms -> everything else defaults."""
        m = extract_metrics({"duration_ms": 500})
        assert m["elapsed_ms"] == 500
        assert m["input_tokens"] == 0
        assert m["output_tokens"] == 0
        assert m["cost_usd"] == 0.0
        assert m["model_id"] == ""
        assert m["context_window"] == 0

    def test_empty_model_usage(self):
        """modelUsage is empty dict -> model_id is empty, context_window=0."""
        m = extract_metrics({"modelUsage": {}})
        assert m["model_id"] == ""
        assert m["context_window"] == 0
        assert m["max_output_tokens"] == 0

    def test_all_fields_present(self):
        """Complete response -> all fields extracted correctly."""
        response = {
            "result": "done",
            "duration_ms": 8000,
            "duration_api_ms": 6000,
            "total_cost_usd": 0.15,
            "usage": {
                "input_tokens": 5000,
                "output_tokens": 2000,
                "cache_creation_input_tokens": 10000,
                "cache_read_input_tokens": 3000,
            },
            "modelUsage": {
                "claude-sonnet-4-20250514": {
                    "contextWindow": 200000,
                    "maxOutputTokens": 16384,
                }
            },
        }
        m = extract_metrics(response)
        assert m["elapsed_ms"] == 8000
        assert m["api_elapsed_ms"] == 6000
        assert m["input_tokens"] == 5000
        assert m["output_tokens"] == 2000
        assert m["cache_creation_tokens"] == 10000
        assert m["cache_read_tokens"] == 3000
        assert m["cost_usd"] == 0.15
        assert m["model_id"] == "claude-sonnet-4-20250514"
        assert m["context_window"] == 200000
        assert m["max_output_tokens"] == 16384


# ── run_claude_tracked DI mode tests ─────────────────────────────────────────


class TestRunClaudeTrackedDI:

    @pytest.mark.asyncio
    async def test_di_runner_returns_result_and_metrics(self):
        """DI runner -> (result, metrics) with elapsed_ms set."""

        async def mock_runner(prompt, cwd=None, timeout=300):
            return "mock result"

        result, metrics = await run_claude_tracked("test", runner=mock_runner)
        assert result == "mock result"
        assert metrics["elapsed_ms"] >= 0
        # All metric keys present
        for key in EMPTY_METRICS:
            assert key in metrics
        # DI mode: only elapsed_ms should be non-zero, rest are zeros
        assert metrics["input_tokens"] == 0
        assert metrics["output_tokens"] == 0
        assert metrics["cost_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_di_runner_exception_propagation(self):
        """DI runner raises exception -> propagated to caller."""
        from supervisor.claude_runner import ClaudeRunnerError

        async def failing_runner(prompt, cwd=None, timeout=300):
            raise ClaudeRunnerError("runner failed")

        with pytest.raises(ClaudeRunnerError, match="runner failed"):
            await run_claude_tracked("test", runner=failing_runner)


# ── run_claude_tracked real mode tests (mocked subprocess) ───────────────────


def _mock_process(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Create a mock async process."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


class TestRunClaudeTrackedReal:

    @pytest.mark.asyncio
    async def test_malformed_json_fallback(self):
        """Non-JSON stdout -> fallback to text result with EMPTY_METRICS."""
        proc = _mock_process(
            stdout=b"This is not JSON at all",
            returncode=0,
        )
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            result, metrics = await run_claude_tracked("test prompt")
            assert result == "This is not JSON at all"
            # Metrics should be empty defaults
            assert metrics["input_tokens"] == 0
            assert metrics["cost_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_valid_json_response(self):
        """Valid JSON from Claude CLI -> result text extracted + metrics parsed."""
        response = {
            "result": "Hello world",
            "duration_ms": 3000,
            "duration_api_ms": 2000,
            "total_cost_usd": 0.05,
            "usage": {
                "input_tokens": 500,
                "output_tokens": 200,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "modelUsage": {
                "claude-opus-4-6[1m]": {
                    "contextWindow": 1000000,
                    "maxOutputTokens": 64000,
                }
            },
        }
        proc = _mock_process(
            stdout=json.dumps(response).encode(),
            returncode=0,
        )
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            result, metrics = await run_claude_tracked("test prompt")
            assert result == "Hello world"
            assert metrics["elapsed_ms"] == 3000
            assert metrics["input_tokens"] == 500
            assert metrics["output_tokens"] == 200
            assert metrics["cost_usd"] == 0.05
            assert metrics["model_id"] == "claude-opus-4-6[1m]"

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self):
        """Real mode: nonzero returncode -> ClaudeRunnerError."""
        from supervisor.claude_runner import ClaudeRunnerError

        proc = _mock_process(
            stdout=b"",
            stderr=b"Authentication failed",
            returncode=1,
        )
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(ClaudeRunnerError, match="exited with code 1"):
                await run_claude_tracked("test prompt")

    @pytest.mark.asyncio
    async def test_model_arg_passed_to_cli(self):
        """model parameter is passed as --model flag to CLI."""
        response = {"result": "ok", "usage": {}, "modelUsage": {}}
        proc = _mock_process(
            stdout=json.dumps(response).encode(),
            returncode=0,
        )
        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = list(args)
            return proc

        with patch("asyncio.create_subprocess_exec", new=capture_exec):
            await run_claude_tracked("test", model="sonnet")

        assert captured_cmd is not None
        assert "--model" in captured_cmd
        assert "sonnet" in captured_cmd
