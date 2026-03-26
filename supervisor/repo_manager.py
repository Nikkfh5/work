"""
supervisor/repo_manager.py — управление bare mirror репозиториями и worktree.

Структура директорий:
    repos_cache/{job}/{alias}.git/         ← bare mirror
    worktrees/{task_id}/{alias}/           ← изолированная копия для задачи
    workers/{job}_worker/workspace/{task_id}/  ← СИМЛИНК → worktrees/{task_id}/

Симлинк нужен для MCP совместимости: Claude запускается из workers/{job}_worker/,
где находится .mcp.json. В CLAUDE.md путь к коду фиксирован как workspace/{task_id}/{alias}/.

Инварианты:
- ensure_mirror: только git clone --mirror или git fetch (идемпотентно)
- prepare_worktree: создаёт новый worktree и симлинк
- cleanup_worktree: удаляет симлинк, затем worktree
- cleanup_old_worktrees: удаляет worktrees старше N дней
"""

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Дефолтные корни директорий (переопределяются в тестах)
DEFAULT_REPOS_CACHE = "repos_cache"
DEFAULT_WORKTREES = "worktrees"
DEFAULT_WORKERS_DIR = "workers"


class RepoManagerError(Exception):
    """Ошибка при работе с репозиторием."""

    pass


class RepoManager:
    """
    Управляет bare mirror репозиториями и worktree-ами для задач.

    Все операции принимают корневые директории параметром для тестируемости.
    """

    def __init__(
        self,
        repos_cache_dir: str = DEFAULT_REPOS_CACHE,
        worktrees_dir: str = DEFAULT_WORKTREES,
        workers_dir: str = DEFAULT_WORKERS_DIR,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.repos_cache = Path(repos_cache_dir)
        self.worktrees = Path(worktrees_dir)
        self.workers = Path(workers_dir)
        self.now_fn = now_fn

    def _mirror_path(self, job: str, alias: str) -> Path:
        """Путь к bare mirror: repos_cache/{job}/{alias}.git"""
        return self.repos_cache / job / f"{alias}.git"

    def _worktree_path(self, task_id: str, alias: str) -> Path:
        """Путь к worktree для задачи: worktrees/{task_id}/{alias}"""
        return self.worktrees / task_id / alias

    def _symlink_path(self, job: str, task_id: str) -> Path:
        """Путь к симлинку воркера: workers/{job}_worker/workspace/{task_id}"""
        return self.workers / f"{job}_worker" / "workspace" / task_id

    def _resolve_base_branch(self, mirror: Path, preferred: str) -> str:
        """
        Найти реальную базовую ветку в mirror.
        Если preferred существует — вернуть её. Иначе вернуть HEAD ветку.
        """
        # Проверяем предпочтительную ветку
        result = self._run_git(
            ["rev-parse", "--verify", preferred],
            cwd=str(mirror),
        )
        if result.returncode == 0:
            return preferred

        # Получаем HEAD ветку
        result = self._run_git(["symbolic-ref", "HEAD"], cwd=str(mirror))
        if result.returncode == 0:
            head_ref = result.stdout.strip()
            return head_ref.replace("refs/heads/", "")

        # Последний fallback — master
        return "master"

    def _run_git(
        self, args: list[str], cwd: Optional[str] = None, env: Optional[dict] = None
    ) -> subprocess.CompletedProcess:
        """Запустить git команду. Никогда shell=True."""
        cmd = ["git"] + args
        logger.debug("git %s (cwd=%s)", " ".join(args), cwd)
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env or os.environ,
        )
        if result.returncode != 0:
            logger.warning(
                "git %s failed (code %d): %s",
                args[0],
                result.returncode,
                result.stderr[:500],
            )
        return result

    def ensure_mirror(
        self,
        job: str,
        alias: str,
        git_url: str,
        clone_strategy: str = "mirror",
        token: Optional[str] = None,
    ) -> Path:
        """
        Создать или обновить bare mirror репозитория.

        Args:
            job:            название работы (job1, job2, ...)
            alias:          псевдоним репо (api, frontend, ...)
            git_url:        URL репозитория
            clone_strategy: "mirror" | "shallow" | "partial" | "sparse"
            token:          git токен для HTTPS URL (подставляется в URL)

        Returns:
            Путь к mirror директории.

        Raises:
            RepoManagerError при ошибке git.
        """
        mirror = self._mirror_path(job, alias)

        # Встраиваем токен в URL если есть
        auth_url = _inject_token(git_url, token) if token else git_url

        if mirror.exists():
            # Обновляем существующий mirror
            logger.info("ensure_mirror: fetching %s/%s", job, alias)
            result = self._run_git(["fetch", "--all"], cwd=str(mirror))
            if result.returncode != 0:
                raise RepoManagerError(
                    f"git fetch failed for {job}/{alias}: {result.stderr[:300]}"
                )
        else:
            # Клонируем
            mirror.parent.mkdir(parents=True, exist_ok=True)
            logger.info(
                "ensure_mirror: cloning %s/%s strategy=%s", job, alias, clone_strategy
            )

            clone_args = _build_clone_args(auth_url, str(mirror), clone_strategy)
            result = self._run_git(clone_args)
            if result.returncode != 0:
                raise RepoManagerError(
                    f"git clone failed for {job}/{alias}: {result.stderr[:300]}"
                )

        logger.info("ensure_mirror: %s/%s ready at %s", job, alias, mirror)
        return mirror

    def prepare_worktree(
        self,
        task_id: str,
        job: str,
        alias: str,
        branch: Optional[str] = None,
        base_branch: str = "main",
    ) -> Path:
        """
        Создать worktree из mirror и симлинк для воркера.

        Структура после вызова:
            worktrees/{task_id}/{alias}/          ← git worktree
            workers/{job}_worker/workspace/{task_id}/ ← symlink → worktrees/{task_id}/

        Args:
            task_id:     ID задачи
            job:         название работы
            alias:       псевдоним репо
            branch:      название ветки для worktree (None → ai/task-{task_id})
            base_branch: базовая ветка

        Returns:
            Путь к worktree директории.
        """
        mirror = self._mirror_path(job, alias)
        if not mirror.exists():
            raise RepoManagerError(
                f"Mirror {mirror} does not exist. Call ensure_mirror() first."
            )

        wt_path = self._worktree_path(task_id, alias)
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        if branch is None:
            branch = f"ai/task-{task_id}"

        # Определяем реальную базовую ветку из mirror (может быть master или main)
        actual_base = self._resolve_base_branch(mirror, base_branch)

        # Создаём worktree с новой веткой от base_branch
        result = self._run_git(
            ["worktree", "add", "-b", branch, str(wt_path), actual_base],
            cwd=str(mirror),
        )
        if result.returncode != 0:
            # Пробуем без -b если ветка уже существует
            result = self._run_git(
                ["worktree", "add", str(wt_path), branch],
                cwd=str(mirror),
            )
            if result.returncode != 0:
                raise RepoManagerError(
                    f"git worktree add failed for task {task_id}/{alias}: {result.stderr[:300]}"
                )

        # Создаём симлинк для MCP совместимости
        symlink = self._symlink_path(job, task_id)
        symlink.parent.mkdir(parents=True, exist_ok=True)

        # Симлинк ведёт на директорию worktrees/{task_id}/
        # (а не на alias), чтобы воркер видел все репо через workspace/{task_id}/{alias}/
        target = wt_path.parent  # worktrees/{task_id}/

        if symlink.exists() or symlink.is_symlink():
            symlink.unlink()

        try:
            if sys.platform == "win32":
                # Windows: используем junction для директорий
                import subprocess as sp

                sp.run(
                    ["cmd", "/c", "mklink", "/J", str(symlink), str(target)],
                    check=True,
                    capture_output=True,
                )
            else:
                symlink.symlink_to(target)
        except Exception as exc:
            logger.warning(
                "prepare_worktree: failed to create symlink %s → %s: %s",
                symlink,
                target,
                exc,
            )

        logger.info(
            "prepare_worktree: task_id=%s alias=%s branch=%s", task_id, alias, branch
        )
        return wt_path

    def cleanup_worktree(self, task_id: str, job: str, alias: str) -> None:
        """
        Удалить симлинк и worktree после завершения задачи.

        Args:
            task_id: ID задачи
            job:     название работы
            alias:   псевдоним репо
        """
        # 1. Удаляем симлинк
        symlink = self._symlink_path(job, task_id)
        if symlink.exists() or symlink.is_symlink():
            try:
                if symlink.is_dir() and not symlink.is_symlink():
                    shutil.rmtree(symlink)
                else:
                    symlink.unlink()
                logger.debug("cleanup_worktree: removed symlink %s", symlink)
            except OSError as exc:
                logger.warning(
                    "cleanup_worktree: failed to remove symlink %s: %s", symlink, exc
                )

        # 2. Удаляем worktree из git
        mirror = self._mirror_path(job, alias)
        wt_path = self._worktree_path(task_id, alias)

        if mirror.exists() and wt_path.exists():
            result = self._run_git(
                ["worktree", "remove", "--force", str(wt_path)],
                cwd=str(mirror),
            )
            if result.returncode != 0:
                logger.warning(
                    "cleanup_worktree: git worktree remove failed: %s",
                    result.stderr[:200],
                )

        # 3. Удаляем директорию worktrees/{task_id}/ если пуста
        task_dir = self.worktrees / task_id
        if task_dir.exists():
            try:
                shutil.rmtree(task_dir)
                logger.debug("cleanup_worktree: removed task dir %s", task_dir)
            except OSError as exc:
                logger.warning("cleanup_worktree: failed to remove task dir: %s", exc)

        logger.info("cleanup_worktree: task_id=%s alias=%s cleaned up", task_id, alias)

    def cleanup_old_worktrees(self, retention_days: int = 7) -> int:
        """
        Удалить worktrees старше retention_days дней.

        Args:
            retention_days: сколько дней хранить worktrees

        Returns:
            Количество удалённых директорий.
        """
        if not self.worktrees.exists():
            return 0

        now = self.now_fn()
        cutoff = now - timedelta(days=retention_days)
        removed = 0

        for task_dir in self.worktrees.iterdir():
            if not task_dir.is_dir():
                continue
            try:
                mtime = datetime.fromtimestamp(
                    task_dir.stat().st_mtime, tz=timezone.utc
                )
                if mtime < cutoff:
                    shutil.rmtree(task_dir)
                    logger.info(
                        "cleanup_old_worktrees: removed %s (mtime=%s)", task_dir, mtime
                    )
                    removed += 1
            except OSError as exc:
                logger.warning(
                    "cleanup_old_worktrees: error processing %s: %s", task_dir, exc
                )

        logger.info("cleanup_old_worktrees: removed %d old worktree(s)", removed)
        return removed


# ── Вспомогательные функции ──────────────────────────────────────────────────


def _inject_token(url: str, token: str) -> str:
    """Встроить токен в HTTPS URL: https://token@host/path."""
    if url.startswith("https://"):
        return f"https://{token}@{url[len('https://') :]}"
    return url


def _build_clone_args(url: str, dest: str, strategy: str) -> list[str]:
    """Построить аргументы git clone по стратегии."""
    if strategy == "mirror":
        return ["clone", "--mirror", url, dest]
    elif strategy == "shallow":
        return ["clone", "--depth", "1", url, dest]
    elif strategy == "partial":
        return ["clone", "--filter=blob:none", url, dest]
    elif strategy == "sparse":
        return ["clone", "--sparse", url, dest]
    else:
        return ["clone", "--mirror", url, dest]
