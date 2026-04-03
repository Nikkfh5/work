"""
supervisor/safe_exec.py — профильный allowlist-runner для внешних команд.

Инварианты:
- Никогда shell=True
- Проверяет команду по COMMAND_PROFILES перед запуском
- Проверяет cwd через realpath (защита от symlink escape)
- Передаёт минимальный env (без секретов)
- Капирует stdout/stderr до MAX_OUTPUT_BYTES

Использование:
    stdout, stderr, returncode = safe_exec(
        cmd=["pytest", "tests/"],
        cwd="/app/worktrees/task-123/api",
        timeout=300,
        allowed_roots=["/app/worktrees/", "/app/workers/"],
    )
"""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Максимальный размер захватываемого вывода (10 MB)
MAX_OUTPUT_BYTES = 10 * 1024 * 1024

# Базовый безопасный env — передаём только нейтральные переменные
SAFE_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "GOPATH",
    "GOROOT",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "NODE_PATH",
    # Windows: без этих переменных subprocess/pytest/asyncio не работают
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "WINDIR",
    "HOMEDRIVE",
    "HOMEPATH",
}

# Паттерны аргументов которые НИКОГДА не разрешены ни в какой команде
BLOCKED_ARG_PATTERNS = [
    "--upload-pack",
    "--exec",
    "mkfs",
    "shutdown",
    "reboot",
]

# Профили команд: cmd[0] → правила проверки
COMMAND_PROFILES: dict[str, dict] = {
    "git": {
        "allowed_subcommands": [
            "clone",
            "fetch",
            "worktree",
            "add",
            "commit",
            "push",
            "diff",
            "log",
            "status",
            "gc",
            "prune",
            "init",
            "checkout",
            "branch",
            "remote",
            "config",
            "rev-parse",
            "show",
            "cat-file",
            "ls-files",
            "stash",
        ],
        "blocked_flags": ["-c", "--upload-pack", "--exec"],
    },
    "pytest": {
        "allowed_forms": ["pytest", "python -m pytest"],
    },
    "python": {
        "allowed_subcommands": ["-m"],
        "allowed_m_modules": ["pytest"],
    },
    "npm": {
        "allowed_subcommands": ["test", "run"],
        "allowed_run_scripts": ["lint", "build", "test"],
    },
    "go": {
        "allowed_subcommands": ["test", "vet", "build"],
    },
    "ruff": {
        "allowed_subcommands": ["check", "format"],
    },
    "pip": {
        "allowed_subcommands": ["install"],
    },
}

# Команды которые НИКОГДА не разрешены
DENYLIST_COMMANDS = {
    "rm",
    "sudo",
    "chmod",
    "chown",
    "dd",
    "mkfs",
    "kill",
    "shutdown",
    "reboot",
    "poweroff",
    "wget",
    "curl",
    "nc",
    "ssh",
    "scp",
    "rsync",
    "docker",
    "kubectl",
}


class SafeExecError(Exception):
    """Команда заблокирована allowlist-ом или нарушает политику."""

    pass


_BLOCKED_ENV_PATTERNS = (
    "TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL",
    "AWS_", "AZURE_", "GCP_",
)

# Env vars that must NEVER be passed (injection/leak vectors)
_DENYLIST_ENV_KEYS = {
    "LD_PRELOAD", "LD_LIBRARY_PATH",
    "HISTFILE", "HISTFILESIZE",
    "DATABASE_URL", "REDIS_URL", "MONGO_URL",
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "TELEGRAM_BOT_TOKEN", "NOTION_TOKEN",
}


def _build_safe_env(env_extra: Optional[dict] = None) -> dict:
    """
    Построить минимальный безопасный env.

    Strict whitelist: передаём ТОЛЬКО ключи из SAFE_ENV_KEYS.
    Denylist как второй слой: блокируем injection vectors.
    """
    # Layer 1: strict whitelist from os.environ
    safe = {k: v for k, v in os.environ.items() if k in SAFE_ENV_KEYS}

    if env_extra:
        for key, val in env_extra.items():
            # Layer 2: denylist — known dangerous env vars
            if key in _DENYLIST_ENV_KEYS:
                logger.warning("safe_exec: blocked denylist env var %s", key)
                continue
            # Layer 3: pattern-based secret detection
            key_upper = key.upper()
            if any(pat in key_upper for pat in _BLOCKED_ENV_PATTERNS):
                logger.warning("safe_exec: blocked secret env var %s", key)
                continue
            safe[key] = val
    return safe


def _check_cmd(cmd: list[str]) -> None:
    """
    Проверить команду по allowlist.

    Raises:
        SafeExecError если команда не разрешена.
    """
    if not cmd:
        raise SafeExecError("safe_exec: empty command")

    program = cmd[0]

    # Абсолютный denylist
    prog_name = Path(program).name
    if prog_name in DENYLIST_COMMANDS:
        raise SafeExecError(f"safe_exec: program {program!r} is in DENYLIST_COMMANDS")

    # Проверка заблокированных аргументов
    for arg in cmd[1:]:
        for blocked in BLOCKED_ARG_PATTERNS:
            if arg == blocked or arg.startswith(blocked + "="):
                raise SafeExecError(f"safe_exec: blocked argument {arg!r} in command")

    # Проверка по профилю
    profile = COMMAND_PROFILES.get(prog_name)
    if profile is None:
        raise SafeExecError(
            f"safe_exec: program {prog_name!r} is not in COMMAND_PROFILES. "
            f"Allowed: {sorted(COMMAND_PROFILES)}"
        )

    args = cmd[1:]

    # Проверка subcommand
    if "allowed_subcommands" in profile and args:
        subcommand = args[0]
        # Разрешаем флаги (начинаются с -) без проверки
        if not subcommand.startswith("-"):
            if subcommand not in profile["allowed_subcommands"]:
                raise SafeExecError(
                    f"safe_exec: subcommand {subcommand!r} not allowed for {prog_name!r}. "
                    f"Allowed: {profile['allowed_subcommands']}"
                )

    # Для git: blocked_flags
    if prog_name == "git" and "blocked_flags" in profile:
        for arg in args:
            for flag in profile["blocked_flags"]:
                if arg == flag or arg.startswith(flag + "="):
                    raise SafeExecError(f"safe_exec: git flag {arg!r} is blocked")

    # Для npm run: проверяем allowed_run_scripts
    if prog_name == "npm" and len(args) >= 2 and args[0] == "run":
        script = args[1]
        allowed_scripts = profile.get("allowed_run_scripts", [])
        if script not in allowed_scripts:
            raise SafeExecError(
                f"safe_exec: npm run script {script!r} not allowed. "
                f"Allowed: {allowed_scripts}"
            )

    # Для python -m: проверяем модуль
    if prog_name == "python" and len(args) >= 2 and args[0] == "-m":
        module = args[1]
        allowed_modules = profile.get("allowed_m_modules", [])
        if module not in allowed_modules:
            raise SafeExecError(
                f"safe_exec: python -m {module!r} not allowed. "
                f"Allowed: {allowed_modules}"
            )


def _check_cwd(cwd: str, allowed_roots: list[str]) -> None:
    """
    Проверить рабочую директорию через realpath (защита от symlink escape).

    Raises:
        SafeExecError если cwd вне допустимых корней.
    """
    try:
        real_cwd = os.path.realpath(cwd)
    except OSError as exc:
        raise SafeExecError(f"safe_exec: cannot resolve cwd {cwd!r}: {exc}")

    for root in allowed_roots:
        real_root = os.path.realpath(root)
        if real_cwd.startswith(real_root):
            return

    raise SafeExecError(
        f"safe_exec: cwd {cwd!r} (real: {real_cwd!r}) is outside allowed_roots={allowed_roots}"
    )


def safe_exec(
    cmd: list[str],
    cwd: str,
    timeout: int = 300,
    env_extra: Optional[dict] = None,
    allowed_roots: Optional[list[str]] = None,
) -> tuple[str, str, int]:
    """
    Выполнить команду в безопасном режиме.

    Проверяет:
    1. cmd[0] в COMMAND_PROFILES и не в DENYLIST
    2. subcommands/flags по профилю
    3. cwd начинается с allowed_roots (через realpath)
    4. Минимальный безопасный env

    Args:
        cmd:           список аргументов команды (никогда строка)
        cwd:           рабочая директория
        timeout:       таймаут в секундах
        env_extra:     дополнительные env переменные (секреты фильтруются)
        allowed_roots: допустимые корни для cwd (None → разрешить любой)

    Returns:
        (stdout, stderr, returncode)

    Raises:
        SafeExecError: команда заблокирована
        subprocess.TimeoutExpired: таймаут истёк
    """
    _check_cmd(cmd)

    if allowed_roots is not None:
        _check_cwd(cwd, allowed_roots)

    safe_env = _build_safe_env(env_extra)

    logger.info("safe_exec cmd=%s cwd=%s timeout=%d", cmd[0], cwd, timeout)

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=safe_env,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "safe_exec timeout cmd=%s cwd=%s timeout=%d",
            cmd[0],
            cwd,
            timeout,
        )
        raise

    # Cap stdout/stderr
    stdout_bytes = proc.stdout or b""
    stderr_bytes = proc.stderr or b""

    truncated = False
    if len(stdout_bytes) > MAX_OUTPUT_BYTES:
        stdout_bytes = stdout_bytes[:MAX_OUTPUT_BYTES]
        truncated = True
    if len(stderr_bytes) > MAX_OUTPUT_BYTES:
        stderr_bytes = stderr_bytes[:MAX_OUTPUT_BYTES]
        truncated = True

    if truncated:
        logger.warning(
            "safe_exec: output truncated at %d bytes cmd=%s",
            MAX_OUTPUT_BYTES,
            cmd[0],
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        logger.warning(
            "safe_exec cmd=%s returncode=%d cwd=%s",
            cmd[0],
            proc.returncode,
            cwd,
        )

    return stdout, stderr, proc.returncode


async def safe_exec_async(
    cmd: list[str],
    cwd: str,
    timeout: int = 300,
    env_extra: Optional[dict] = None,
    allowed_roots: Optional[list[str]] = None,
) -> tuple[str, str, int]:
    """
    Асинхронная версия safe_exec. Использует asyncio.create_subprocess_exec.

    Те же проверки что и sync версия.
    """
    _check_cmd(cmd)

    if allowed_roots is not None:
        _check_cwd(cwd, allowed_roots)

    safe_env = _build_safe_env(env_extra)

    logger.info("safe_exec_async cmd=%s cwd=%s timeout=%d", cmd[0], cwd, timeout)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=safe_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        logger.error("safe_exec_async timeout cmd=%s cwd=%s", cmd[0], cwd)
        raise

    stdout_bytes = stdout_bytes or b""
    stderr_bytes = stderr_bytes or b""

    if len(stdout_bytes) > MAX_OUTPUT_BYTES:
        stdout_bytes = stdout_bytes[:MAX_OUTPUT_BYTES]
        logger.warning("safe_exec_async: stdout truncated cmd=%s", cmd[0])
    if len(stderr_bytes) > MAX_OUTPUT_BYTES:
        stderr_bytes = stderr_bytes[:MAX_OUTPUT_BYTES]
        logger.warning("safe_exec_async: stderr truncated cmd=%s", cmd[0])

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        logger.warning(
            "safe_exec_async cmd=%s returncode=%d",
            cmd[0],
            proc.returncode,
        )

    return stdout, stderr, proc.returncode
