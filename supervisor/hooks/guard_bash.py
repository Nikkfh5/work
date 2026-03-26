"""
supervisor/hooks/guard_bash.py — read-only Bash guard для воркеров.

Claude Code Hook (PreToolUse) для инструмента Bash.
Читает tool input из stdin как JSON, проверяет команду:
  - exit(0): разрешено (read-only команды)
  - exit(2): заблокировано

Принцип: разрешаем диагностические read-only команды,
блокируем всё опасное. Неизвестная команда = запрет по умолчанию.

Протокол Hook:
  stdin: {"tool_name": "Bash", "tool_input": {"command": "..."}}
  exit(0): разрешить
  exit(2): заблокировать (Claude увидит сообщение из stdout)

Использование (в settings.json воркера):
  "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
    "command": "python /app/supervisor/hooks/guard_bash.py"}]}]
"""

import json
import logging
import sys

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s guard_bash %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("guard_bash")

# ── Allowlist читающих команд ─────────────────────────────────────────────────

ALLOWED_PROGRAMS = {
    "pwd",
    "ls",
    "find",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "wc",
    "echo",
    "stat",
    "file",
    "git",
}

# Для git — разрешены только read-only subcommands
ALLOWED_GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "ls-files",
    "branch",
    "rev-parse",
    "cat-file",
    "describe",
}

# ── Denylist программ ─────────────────────────────────────────────────────────

BLOCKED_PROGRAMS = {
    "git add",
    "git commit",
    "git push",
    "git reset",
    "git checkout",
    "git merge",
    "git rebase",
    "git stash",
    "pytest",
    "npm",
    "go",
    "make",
    "pip",
    "pip3",
    "rm",
    "mv",
    "cp",
    "chmod",
    "chown",
    "sudo",
    "curl",
    "wget",
    "ssh",
    "nc",
    "python",
    "python3",
    "node",
    "ruby",
    "touch",
    "mkdir",
    "mkfifo",
    "kill",
    "killall",
    "systemctl",
    "service",
    "docker",
    "kubectl",
}

# Shell операторы которые превращают read-only в write
BLOCKED_SHELL_OPERATORS = ["&&", "||", ";", "|", ">", "<", "`", "$(", "${"]


def _is_blocked_by_operator(command: str) -> bool:
    """Проверить наличие shell операторов."""
    for op in BLOCKED_SHELL_OPERATORS:
        if op in command:
            return True
    return False


def _get_program(command: str) -> str:
    """Извлечь имя программы из команды."""
    stripped = command.strip()
    parts = stripped.split()
    if not parts:
        return ""
    return parts[0]


def _get_args(command: str) -> list[str]:
    """Извлечь аргументы команды."""
    parts = command.strip().split()
    return parts[1:] if len(parts) > 1 else []


def check_command(command: str) -> tuple[bool, str]:
    """
    Проверить команду.

    Args:
        command: строка команды из Claude Bash tool

    Returns:
        (allowed: bool, reason: str)
        allowed=True → разрешить (exit 0)
        allowed=False → заблокировать (exit 2) с reason
    """
    if not command or not command.strip():
        return False, "empty command"

    # 1. Проверяем shell операторы — блокируем сразу
    if _is_blocked_by_operator(command):
        op_found = next((op for op in BLOCKED_SHELL_OPERATORS if op in command), "?")
        return False, f"shell operator {op_found!r} not allowed"

    program = _get_program(command)
    args = _get_args(command)

    # 2. Проверяем denylist программ
    for blocked in BLOCKED_PROGRAMS:
        blocked_parts = blocked.split()
        if program == blocked_parts[0]:
            if len(blocked_parts) == 1:
                return False, f"program {program!r} is blocked"
            elif args and args[0] == blocked_parts[1]:
                return False, f"command {blocked!r} is blocked"

    # 3. Специальная обработка git
    if program == "git":
        if not args:
            return False, "bare 'git' without subcommand"
        subcommand = args[0]
        if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
            return False, f"git subcommand {subcommand!r} not in allowlist"
        return True, f"git {subcommand} allowed"

    # 4. Проверяем allowlist программ
    if program not in ALLOWED_PROGRAMS:
        return False, f"program {program!r} not in allowlist"

    return True, f"program {program!r} allowed"


def main() -> int:
    """
    Точка входа hook.

    Читает JSON из stdin, проверяет команду, возвращает exit code.
    exit(0) = разрешить, exit(2) = заблокировать.
    """
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            # Нет данных — разрешаем (осторожный fallback)
            return 0

        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("guard_bash: failed to parse stdin: %s", exc)
        return 0  # При ошибке чтения — разрешаем (не блокируем случайно)

    # Извлекаем команду
    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        # Не Bash или нет команды
        return 0

    allowed, reason = check_command(command)

    if allowed:
        logger.warning("guard_bash ALLOW cmd=%r reason=%s", command[:100], reason)
        return 0
    else:
        logger.warning("guard_bash BLOCK cmd=%r reason=%s", command[:100], reason)
        # Сообщение для Claude — выводим в stdout
        print(f"[guard_bash] Command blocked: {reason}", flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
