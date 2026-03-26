"""
tests/test_repo_manager.py — тесты для supervisor/repo_manager.py

Использует реальный git в tmp_path.
Запуск: pytest tests/test_repo_manager.py -v
"""

import subprocess
import sys
import pytest
from datetime import datetime, timedelta, timezone

from supervisor.repo_manager import (
    RepoManager,
    RepoManagerError,
    _inject_token,
    _build_clone_args,
)


def has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(not has_git(), reason="git not available")


# ── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def source_repo(tmp_path):
    """Создаём bare source репозиторий с одним коммитом."""
    repo = tmp_path / "source_repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    # Создаём файл и коммит
    (repo / "hello.txt").write_text("hello world")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def manager(tmp_path):
    """RepoManager с изолированными директориями в tmp_path."""
    return RepoManager(
        repos_cache_dir=str(tmp_path / "repos_cache"),
        worktrees_dir=str(tmp_path / "worktrees"),
        workers_dir=str(tmp_path / "workers"),
    )


# ── Тесты _inject_token ──────────────────────────────────────────────────────


def test_inject_token_https():
    url = "https://github.com/org/repo.git"
    result = _inject_token(url, "ghp_mytoken")
    assert result == "https://ghp_mytoken@github.com/org/repo.git"


def test_inject_token_non_https_unchanged():
    url = "git@github.com:org/repo.git"
    result = _inject_token(url, "token")
    assert result == url


# ── Тесты _build_clone_args ──────────────────────────────────────────────────


def test_build_clone_args_mirror():
    args = _build_clone_args("https://host/repo.git", "/tmp/dest", "mirror")
    assert "--mirror" in args


def test_build_clone_args_shallow():
    args = _build_clone_args("https://host/repo.git", "/tmp/dest", "shallow")
    assert "--depth" in args
    assert "1" in args


def test_build_clone_args_partial():
    args = _build_clone_args("https://host/repo.git", "/tmp/dest", "partial")
    assert "--filter=blob:none" in args


def test_build_clone_args_sparse():
    args = _build_clone_args("https://host/repo.git", "/tmp/dest", "sparse")
    assert "--sparse" in args


# ── Тесты ensure_mirror ──────────────────────────────────────────────────────


def test_ensure_mirror_creates_bare_repo(tmp_path, source_repo, manager):
    """ensure_mirror создаёт bare репозиторий."""
    mirror_path = manager.ensure_mirror(
        job="job1",
        alias="api",
        git_url=str(source_repo),
        clone_strategy="mirror",
    )
    assert mirror_path.exists()
    # bare repo содержит HEAD файл
    assert (mirror_path / "HEAD").exists()


def test_ensure_mirror_idempotent(tmp_path, source_repo, manager):
    """Повторный вызов ensure_mirror — только fetch, не ошибка."""
    manager.ensure_mirror(job="job1", alias="api", git_url=str(source_repo))
    # Второй вызов
    manager.ensure_mirror(job="job1", alias="api", git_url=str(source_repo))
    mirror = manager._mirror_path("job1", "api")
    assert mirror.exists()


# ── Тесты prepare_worktree ───────────────────────────────────────────────────


def test_prepare_worktree_creates_directory(tmp_path, source_repo, manager):
    """prepare_worktree создаёт рабочую директорию."""
    manager.ensure_mirror(job="job1", alias="api", git_url=str(source_repo))
    wt = manager.prepare_worktree(task_id="task-001", job="job1", alias="api")
    assert wt.exists()
    assert wt.is_dir()


def test_prepare_worktree_has_files(tmp_path, source_repo, manager):
    """В worktree есть файлы из репозитория."""
    manager.ensure_mirror(job="job1", alias="api", git_url=str(source_repo))
    wt = manager.prepare_worktree(task_id="task-002", job="job1", alias="api")
    assert (wt / "hello.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_prepare_worktree_creates_symlink(tmp_path, source_repo, manager):
    """prepare_worktree создаёт симлинк в workers/{job}_worker/workspace/."""
    manager.ensure_mirror(job="job1", alias="api", git_url=str(source_repo))
    manager.prepare_worktree(task_id="task-003", job="job1", alias="api")
    symlink = manager._symlink_path("job1", "task-003")
    assert symlink.exists() or symlink.is_symlink()


def test_prepare_worktree_requires_mirror(tmp_path, manager):
    """prepare_worktree без предварительного ensure_mirror — ошибка."""
    with pytest.raises(RepoManagerError, match="Mirror"):
        manager.prepare_worktree(task_id="task-999", job="job1", alias="api")


# ── Тесты cleanup_worktree ───────────────────────────────────────────────────


def test_cleanup_worktree_removes_directory(tmp_path, source_repo, manager):
    """cleanup_worktree удаляет директорию worktree."""
    manager.ensure_mirror(job="job1", alias="api", git_url=str(source_repo))
    manager.prepare_worktree(task_id="task-004", job="job1", alias="api")
    manager.cleanup_worktree(task_id="task-004", job="job1", alias="api")

    task_dir = manager.worktrees / "task-004"
    assert not task_dir.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_cleanup_worktree_removes_symlink(tmp_path, source_repo, manager):
    """cleanup_worktree удаляет симлинк."""
    manager.ensure_mirror(job="job1", alias="api", git_url=str(source_repo))
    manager.prepare_worktree(task_id="task-005", job="job1", alias="api")
    manager.cleanup_worktree(task_id="task-005", job="job1", alias="api")

    symlink = manager._symlink_path("job1", "task-005")
    assert not symlink.exists()


# ── Тесты cleanup_old_worktrees ──────────────────────────────────────────────


def test_cleanup_old_worktrees_removes_old(tmp_path, manager):
    """cleanup_old_worktrees удаляет директории старше N дней."""
    old_dir = manager.worktrees / "old-task"
    old_dir.mkdir(parents=True)

    # Используем фиксированное время — "сейчас" = через 10 дней после создания
    future = datetime.now(timezone.utc) + timedelta(days=10)

    def future_now():
        return future

    manager_with_future_now = RepoManager(
        repos_cache_dir=str(tmp_path / "repos_cache"),
        worktrees_dir=str(tmp_path / "worktrees"),
        workers_dir=str(tmp_path / "workers"),
        now_fn=future_now,
    )

    removed = manager_with_future_now.cleanup_old_worktrees(retention_days=7)
    assert removed == 1
    assert not old_dir.exists()


def test_cleanup_old_worktrees_keeps_recent(tmp_path, manager):
    """cleanup_old_worktrees не трогает свежие директории."""
    new_dir = manager.worktrees / "new-task"
    new_dir.mkdir(parents=True)

    removed = manager.cleanup_old_worktrees(retention_days=7)
    assert removed == 0
    assert new_dir.exists()


def test_cleanup_old_worktrees_no_worktrees_dir(tmp_path):
    """Если директория worktrees не существует — 0 удалений, не ошибка."""
    manager = RepoManager(
        repos_cache_dir=str(tmp_path / "repos_cache"),
        worktrees_dir=str(tmp_path / "nonexistent"),
        workers_dir=str(tmp_path / "workers"),
    )
    removed = manager.cleanup_old_worktrees()
    assert removed == 0
