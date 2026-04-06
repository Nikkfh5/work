"""
tests/test_claude_runner.py — Tests for supervisor/claude_runner.py.

Tests mock asyncio.create_subprocess_exec to avoid calling real Claude CLI.
Cover: success, nonzero exit, timeout, FileNotFoundError, large output,
       extra_args, run_worker_task delegation, run_supervisor_reasoning delegation.

Запуск: pytest tests/test_claude_runner.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supervisor.claude_runner import (
    ClaudeRunnerError,
    run_claude,
    run_supervisor_reasoning,
    run_worker_task,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_process(stdout: bytes = b"ok", stderr: bytes = b"", returncode: int = 0):
    """Create a mock Process with communicate() returning (stdout, stderr)."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


# ── run_claude tests ─────────────────────────────────────────────────────────


class TestRunClaude:

    @pytest.mark.asyncio
    async def test_success_returns_stdout(self):
        """run_claude returns decoded stdout on success (returncode=0)."""
        proc = _mock_process(stdout=b"Hello from Claude", returncode=0)
        with patch("supervisor.claude_runner.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=proc)
            mock_aio.wait_for = AsyncMock(return_value=(b"Hello from Claude", b""))
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.TimeoutError = asyncio.TimeoutError

            # Directly patch at module level for cleaner test
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            result = await run_claude("test prompt")
            assert result == "Hello from Claude"

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_error(self):
        """run_claude raises ClaudeRunnerError when returncode != 0."""
        proc = _mock_process(
            stdout=b"", stderr=b"Error: model overloaded", returncode=1
        )
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(ClaudeRunnerError, match="exited with code 1"):
                await run_claude("test prompt")

    @pytest.mark.asyncio
    async def test_timeout_raises_and_kills_process(self):
        """run_claude raises ClaudeRunnerError on timeout and calls proc.kill()."""
        proc = _mock_process()

        async def slow_communicate():
            await asyncio.sleep(10)  # will be interrupted by timeout
            return (b"", b"")

        proc.communicate = slow_communicate

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(ClaudeRunnerError, match="timed out"):
                await run_claude("test prompt", timeout=0.01)
            proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_file_not_found_raises_error(self):
        """run_claude raises ClaudeRunnerError when claude CLI binary not found."""
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("No such file")),
        ):
            with pytest.raises(ClaudeRunnerError, match="not found"):
                await run_claude("test prompt")

    @pytest.mark.asyncio
    async def test_large_output_100k(self):
        """run_claude handles large (100KB) stdout without truncation."""
        large_output = b"A" * 100_000
        proc = _mock_process(stdout=large_output, returncode=0)
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            result = await run_claude("big prompt")
            assert len(result) == 100_000

    @pytest.mark.asyncio
    async def test_extra_args_passed_to_subprocess(self):
        """extra_args are appended to the command list."""
        proc = _mock_process(stdout=b"result", returncode=0)
        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = list(args)
            return proc

        with patch(
            "asyncio.create_subprocess_exec",
            new=capture_exec,
        ):
            await run_claude(
                "test prompt",
                extra_args=["--output-format", "json", "--model", "opus"],
            )

        assert captured_cmd is not None
        assert "--output-format" in captured_cmd
        assert "json" in captured_cmd
        assert "--model" in captured_cmd
        assert "opus" in captured_cmd

    @pytest.mark.asyncio
    async def test_cwd_passed_to_subprocess(self):
        """cwd parameter is forwarded to create_subprocess_exec."""
        proc = _mock_process(stdout=b"ok", returncode=0)
        captured_kwargs = {}

        async def capture_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return proc

        with patch(
            "asyncio.create_subprocess_exec",
            new=capture_exec,
        ):
            await run_claude("test", cwd="/tmp/worker_dir")

        assert captured_kwargs.get("cwd") == "/tmp/worker_dir"

    @pytest.mark.asyncio
    async def test_stderr_logged_but_not_raised_on_success(self):
        """Non-empty stderr with returncode=0 does NOT raise, returns stdout."""
        proc = _mock_process(
            stdout=b"answer", stderr=b"warning: deprecated", returncode=0
        )
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            result = await run_claude("test")
            assert result == "answer"


# ── run_worker_task tests ────────────────────────────────────────────────────


class TestRunWorkerTask:

    @pytest.mark.asyncio
    async def test_delegates_to_run_claude_with_cwd(self):
        """run_worker_task calls run_claude with formatted prompt and worker_dir as cwd."""
        proc = _mock_process(stdout=b"task done", returncode=0)
        captured_kwargs = {}

        async def capture_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return proc

        with patch(
            "asyncio.create_subprocess_exec",
            new=capture_exec,
        ):
            result = await run_worker_task(
                worker_id="job1_worker",
                task_description="fix the bug",
                worker_dir="/tmp/worker1",
            )
            assert result == "task done"
            assert captured_kwargs["cwd"] == "/tmp/worker1"


# ── run_supervisor_reasoning tests ───────────────────────────────────────────


class TestRunSupervisorReasoning:

    @pytest.mark.asyncio
    async def test_delegates_to_run_claude_without_cwd(self):
        """run_supervisor_reasoning calls run_claude with cwd=None."""
        proc = _mock_process(stdout=b"analysis result", returncode=0)
        captured_kwargs = {}

        async def capture_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return proc

        with patch(
            "asyncio.create_subprocess_exec",
            new=capture_exec,
        ):
            result = await run_supervisor_reasoning("analyze this task")
            assert result == "analysis result"
            assert captured_kwargs.get("cwd") is None
