"""
tests/test_safe_exec.py — тесты для supervisor/safe_exec.py

Запуск: pytest tests/test_safe_exec.py -v
"""

import os
import sys
import pytest
from pathlib import Path
from supervisor.safe_exec import safe_exec, SafeExecError, _check_cmd, _check_cwd


# ── Вспомогательные утилиты ──────────────────────────────────────────────────

def is_unix():
    return sys.platform != "win32"


# ── Тесты _check_cmd ─────────────────────────────────────────────────────────

def test_check_cmd_allowed_git_status():
    """git status — разрешена."""
    _check_cmd(["git", "status"])  # не должно бросать


def test_check_cmd_allowed_pytest():
    _check_cmd(["pytest", "tests/"])


def test_check_cmd_allowed_ruff_check():
    _check_cmd(["ruff", "check", "."])


def test_check_cmd_allowed_ruff_format():
    _check_cmd(["ruff", "format", "."])


def test_check_cmd_allowed_go_test():
    _check_cmd(["go", "test", "./..."])


def test_check_cmd_allowed_npm_test():
    _check_cmd(["npm", "test"])


def test_check_cmd_allowed_npm_run_lint():
    _check_cmd(["npm", "run", "lint"])


def test_check_cmd_denylist_rm_raises():
    """rm — всегда блокируется."""
    with pytest.raises(SafeExecError, match="DENYLIST"):
        _check_cmd(["rm", "-rf", "/"])


def test_check_cmd_denylist_sudo_raises():
    with pytest.raises(SafeExecError):
        _check_cmd(["sudo", "apt", "install", "something"])


def test_check_cmd_denylist_curl_raises():
    with pytest.raises(SafeExecError):
        _check_cmd(["curl", "http://evil.com"])


def test_check_cmd_unknown_program_raises():
    """Неизвестная программа — блокируется."""
    with pytest.raises(SafeExecError, match="not in COMMAND_PROFILES"):
        _check_cmd(["make", "build"])


def test_check_cmd_git_blocked_flag():
    """git с --exec — блокируется."""
    with pytest.raises(SafeExecError, match="blocked"):
        _check_cmd(["git", "--exec", "malicious"])


def test_check_cmd_git_blocked_upload_pack():
    with pytest.raises(SafeExecError, match="blocked"):
        _check_cmd(["git", "--upload-pack", "something"])


def test_check_cmd_npm_run_disallowed_script():
    """npm run deploy — не разрешён."""
    with pytest.raises(SafeExecError, match="npm run script"):
        _check_cmd(["npm", "run", "deploy"])


def test_check_cmd_git_allowed_subcommands():
    """Все разрешённые git subcommands."""
    for sub in ["clone", "fetch", "add", "commit", "push", "diff", "log", "status", "gc"]:
        _check_cmd(["git", sub])  # не должно бросать


def test_check_cmd_git_disallowed_subcommand():
    """git archive — не разрешён."""
    with pytest.raises(SafeExecError, match="subcommand"):
        _check_cmd(["git", "archive", "HEAD"])


def test_check_cmd_empty_raises():
    with pytest.raises(SafeExecError, match="empty"):
        _check_cmd([])


# ── Тесты _check_cwd ─────────────────────────────────────────────────────────

def test_check_cwd_within_allowed_root(tmp_path):
    """Директория внутри allowed_roots — разрешена."""
    allowed = str(tmp_path)
    subdir = tmp_path / "worktrees" / "task-123"
    subdir.mkdir(parents=True)
    _check_cwd(str(subdir), [allowed])  # не должно бросать


def test_check_cwd_outside_allowed_roots_raises(tmp_path):
    """Директория вне allowed_roots — блокируется."""
    other = tmp_path / "other"
    other.mkdir()
    allowed = str(tmp_path / "worktrees")
    with pytest.raises(SafeExecError, match="outside allowed_roots"):
        _check_cwd(str(other), [allowed])


@pytest.mark.skipif(not is_unix(), reason="symlinks on unix only")
def test_check_cwd_symlink_escape_blocked(tmp_path):
    """Symlink ведущий за пределы allowed_roots — блокируется."""
    # Создаём структуру:
    # /tmp/work/worktrees/task/ → symlink → /tmp/outside/
    outside = tmp_path / "outside"
    outside.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    link = worktrees / "malicious"
    link.symlink_to(outside)

    allowed = [str(worktrees) + "/task-safe"]
    with pytest.raises(SafeExecError, match="outside allowed_roots"):
        _check_cwd(str(link), allowed)


# ── Тесты safe_exec с реальными командами ────────────────────────────────────

def test_safe_exec_blocked_rm(tmp_path):
    """safe_exec(['rm', '-rf', '/'], ...) → SafeExecError."""
    with pytest.raises(SafeExecError):
        safe_exec(["rm", "-rf", "/"], cwd=str(tmp_path))


def test_safe_exec_blocked_sudo(tmp_path):
    with pytest.raises(SafeExecError):
        safe_exec(["sudo", "ls"], cwd=str(tmp_path))


def test_safe_exec_blocked_unknown_program(tmp_path):
    with pytest.raises(SafeExecError):
        safe_exec(["make", "all"], cwd=str(tmp_path))


def test_safe_exec_cwd_check_works(tmp_path):
    """safe_exec с allowed_roots → блокирует cwd вне roots."""
    allowed = [str(tmp_path / "worktrees")]
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    with pytest.raises(SafeExecError, match="outside allowed_roots"):
        safe_exec(["git", "status"], cwd=str(other_dir), allowed_roots=allowed)


@pytest.mark.skipif(not is_unix(), reason="git available on unix in CI")
def test_safe_exec_git_status_in_repo(tmp_path):
    """git status в git-репо — выполняется успешно."""
    import subprocess as sp
    sp.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    stdout, stderr, code = safe_exec(
        ["git", "status"],
        cwd=str(tmp_path),
        allowed_roots=[str(tmp_path)],
    )
    assert code == 0


def test_safe_exec_no_secret_env_passed(tmp_path, monkeypatch):
    """Секреты из os.environ не передаются в процесс."""
    monkeypatch.setenv("GIT_TOKEN_JOB1", "ghp_secret_token")
    # Запускаем python чтобы проверить окружение (но python -m pytest не разрешён без context)
    # Просто проверяем что _build_safe_env не содержит секрет
    from supervisor.safe_exec import _build_safe_env
    env = _build_safe_env(None)
    assert "GIT_TOKEN_JOB1" not in env
    assert "ghp_secret_token" not in env.values()


def test_safe_exec_env_extra_secret_filtered(tmp_path, monkeypatch):
    """Секреты из env_extra тоже фильтруются."""
    from supervisor.safe_exec import _build_safe_env
    env = _build_safe_env({"MY_TOKEN": "secret123", "PATH": "/usr/bin"})
    assert "MY_TOKEN" not in env
    assert "secret123" not in env.values()
    assert "PATH" in env


def test_safe_exec_python_m_pytest_allowed(tmp_path):
    """python -m pytest — разрешена."""
    _check_cmd(["python", "-m", "pytest", "tests/"])


def test_safe_exec_python_m_unknown_blocked(tmp_path):
    """python -m evil_module — блокируется."""
    with pytest.raises(SafeExecError, match="not allowed"):
        _check_cmd(["python", "-m", "evil_module"])
