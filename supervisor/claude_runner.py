"""
supervisor/claude_runner.py — обёртка для запуска claude CLI как subprocess.

Все AI-вызовы в системе идут через этот модуль.
Используется подписка Claude Code Max (без Anthropic API).
"""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH", "claude")


class ClaudeRunnerError(Exception):
    pass


async def run_claude(
    prompt: str,
    cwd: Optional[str] = None,
    timeout: int = 300,
    extra_args: Optional[list[str]] = None,
) -> str:
    """
    Запустить claude CLI с промптом и вернуть результат.

    Args:
        prompt: задание для Claude
        cwd: рабочая директория (папка воркера)
        timeout: таймаут в секундах (default 5 мин)
        extra_args: дополнительные флаги для claude

    Returns:
        stdout ответ claude

    Raises:
        ClaudeRunnerError: если claude вернул ошибку или таймаут
    """
    cmd = [CLAUDE_CLI, "--print", prompt, "--no-interactive"]
    if extra_args:
        cmd.extend(extra_args)

    logger.info("Running claude in %s (timeout=%ds)", cwd or ".", timeout)
    logger.debug("Prompt: %s...", prompt[:100])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise ClaudeRunnerError(
            f"claude CLI timed out after {timeout}s in {cwd}"
        )
    except FileNotFoundError:
        raise ClaudeRunnerError(
            f"claude CLI not found at {CLAUDE_CLI!r}. "
            "Install Claude Code and run 'claude auth login'"
        )

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        logger.error("claude CLI error (code %d): %s", proc.returncode, stderr_text)
        raise ClaudeRunnerError(
            f"claude exited with code {proc.returncode}: {stderr_text[:500]}"
        )

    if stderr_text:
        logger.debug("claude stderr: %s", stderr_text[:200])

    logger.info("claude finished, output: %d chars", len(stdout_text))
    return stdout_text


async def run_worker_task(worker_id: str, task_description: str, worker_dir: str) -> str:
    """
    Запустить задачу для конкретного воркера в его рабочей директории.

    Воркер читает CLAUDE.md, выполняет задачу, коммитит код.
    Результат пишет в worker_result.txt.
    """
    prompt = f"""Задание от супервайзора:

{task_description}

Инструкции:
1. Выполни задание согласно CLAUDE.md
2. Запиши результат в worker_result.txt (формат: ГОТОВО: описание + ссылка на коммит)
3. Если нужно уточнение — запиши в worker_question.txt (формат: ВОПРОС: ...)
4. Если заблокирован — запиши в worker_result.txt (формат: BLOCKED: причина)
"""
    return await run_claude(prompt, cwd=worker_dir)


async def run_supervisor_reasoning(prompt: str) -> str:
    """
    Использовать claude для сложных решений супервайзора:
    суммаризация, анализ статуса, составление дайджеста.
    """
    return await run_claude(prompt, cwd=None)
