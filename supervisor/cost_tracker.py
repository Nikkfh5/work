"""
supervisor/cost_tracker.py — Tracked Claude CLI runner with cost/token metrics.

Wrapper вокруг Claude CLI: вызывает с --output-format json,
парсит ответ, извлекает result + полные метрики (токены, стоимость, время).

Инварианты:
- НЕ трогает claude_runner.py (запрещённый файл)
- При DI runner (тесты) — замеряет только время
- Real mode: subprocess с --output-format json
- Нет shell=True
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Empty metrics template
EMPTY_METRICS: dict = {
    "elapsed_ms": 0,
    "api_elapsed_ms": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_tokens": 0,
    "cache_read_tokens": 0,
    "cost_usd": 0.0,
    "model_id": "",
    "context_window": 0,
    "max_output_tokens": 0,
}


async def run_claude_tracked(
    prompt: str,
    cwd: Optional[str] = None,
    timeout: int = 300,
    runner: Optional[Callable] = None,
) -> tuple[str, dict]:
    """
    Run Claude CLI and return (result_text, metrics_dict).

    If runner is injected (DI for tests), calls it directly and measures elapsed time.
    Otherwise, calls Claude CLI with --output-format json for full metrics.

    Args:
        prompt: задание для Claude
        cwd: рабочая директория
        timeout: таймаут в секундах
        runner: DI replacement for run_claude (tests)

    Returns:
        (result_text, metrics) where metrics contains:
        elapsed_ms, api_elapsed_ms, input_tokens, output_tokens,
        cache_creation_tokens, cache_read_tokens, cost_usd, model_id,
        context_window, max_output_tokens

    Raises:
        ClaudeRunnerError: при ошибке CLI
    """
    if runner:
        # DI mode (tests): call injected runner, measure time only
        start = time.time()
        result = await runner(prompt, cwd=cwd, timeout=timeout)
        elapsed = int((time.time() - start) * 1000)
        metrics = {**EMPTY_METRICS, "elapsed_ms": elapsed}
        return result, metrics

    # Real mode: Claude CLI with --output-format json
    return await _run_claude_json(prompt, cwd, timeout)


async def _run_claude_json(
    prompt: str,
    cwd: Optional[str] = None,
    timeout: int = 300,
) -> tuple[str, dict]:
    """
    Run Claude CLI with --output-format json and parse the response.

    Returns:
        (result_text, metrics_dict)
    """
    from supervisor.claude_runner import CLAUDE_CLI, ClaudeRunnerError

    cmd = [CLAUDE_CLI, "--print", "--output-format", "json", prompt]

    logger.info("cost_tracker: running claude in %s (timeout=%ds)", cwd or ".", timeout)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise ClaudeRunnerError(f"claude CLI timed out after {timeout}s in {cwd}")
    except FileNotFoundError:
        raise ClaudeRunnerError(
            f"claude CLI not found at {CLAUDE_CLI!r}. "
            "Install Claude Code and run 'claude auth login'"
        )

    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        logger.error("cost_tracker: claude error (code %d): %s", proc.returncode, stderr_text)
        raise ClaudeRunnerError(
            f"claude exited with code {proc.returncode}: {stderr_text[:500]}"
        )

    # Parse JSON response
    try:
        response = json.loads(stdout_text)
    except json.JSONDecodeError:
        # Fallback: maybe --output-format json not supported in this version
        logger.warning("cost_tracker: failed to parse JSON response, falling back to text")
        return stdout_text, {**EMPTY_METRICS}

    result_text = response.get("result", "")
    metrics = extract_metrics(response)

    logger.info(
        "cost_tracker: done in %dms, %d input + %d output tokens, $%.4f",
        metrics["elapsed_ms"],
        metrics["input_tokens"],
        metrics["output_tokens"],
        metrics["cost_usd"],
    )

    return result_text, metrics


def extract_metrics(response: dict) -> dict:
    """
    Extract cost/token metrics from Claude CLI JSON response.

    Args:
        response: parsed JSON from claude --output-format json

    Returns:
        dict with all metric fields
    """
    usage = response.get("usage", {})
    model_usage = response.get("modelUsage", {})
    first_model = next(iter(model_usage.values()), {})

    return {
        "elapsed_ms": response.get("duration_ms", 0),
        "api_elapsed_ms": response.get("duration_api_ms", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cost_usd": response.get("total_cost_usd", 0.0),
        "model_id": next(iter(model_usage.keys()), ""),
        "context_window": first_model.get("contextWindow", 0),
        "max_output_tokens": first_model.get("maxOutputTokens", 0),
    }


def accumulate_metrics(ctx_metrics: dict, new_metrics: dict) -> None:
    """
    Accumulate metrics on WorkerContext (in-place update via ctx attributes).

    Usage:
        ctx.cumulative_cost_usd += metrics.get("cost_usd", 0)
        ctx.cumulative_input_tokens += metrics.get("input_tokens", 0)
        ...
    This is a helper for readability — callers use it after each runner call.
    """
    # This is intentionally a documentation helper, not a function.
    # Callers accumulate directly on ctx fields for simplicity.
    pass
