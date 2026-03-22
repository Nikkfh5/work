"""
supervisor/main.py — asyncio orchestration, точка входа системы.

Порядок старта:
  1. load .env
  2. config_validator.load_and_validate() — fail-fast
  3. init_db + apply_migrations
  4. Инициализация TelegramHandler, EmailHandler
  5. Запуск asyncio задач (polling, dispatch, heartbeat, scheduled)
  6. Ожидание shutdown (SIGTERM/SIGINT)
  7. Graceful shutdown: cancel tasks, release stale leases, stop bot

Инварианты:
  - config_validator запускается ПЕРВЫМ
  - supervisor — единственный кто пишет клиенту (через tg_handler)
  - Все asyncio задачи реагируют на _shutdown_event
  - Нет shell=True, нет глобального состояния кроме _shutdown_event / _running_tasks
"""

import asyncio
import logging
import logging.handlers
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from integrations.email_handler import EmailHandler
from integrations.telegram_handler import TelegramHandler
from storage.db import get_conn, init_db
from storage.migrate import apply_migrations
from supervisor.config_validator import load_and_validate
from supervisor.lease_manager import acquire_lease, release_lease, release_stale
from supervisor.repo_manager import RepoManager
from supervisor.safe_exec import safe_exec

logger = logging.getLogger(__name__)

# Коды ошибок (see CLAUDE.md)
E_JSON_INVALID = "json_invalid"
E_JSON_SCHEMA = "json_schema_invalid"
E_SAFEEXEC_TIMEOUT = "safeexec_timeout"
E_WORKER_CRASH = "worker_crash"
E_GIT_PUSH_FAIL = "git_push_failed"
E_REVIEW_EXHAUSTED = "review_exhausted"
E_REVIEWER_CRASH = "reviewer_crash"


def _worker_to_job(worker_id: str) -> str:
    """Извлечь имя job из worker_id: 'job1_worker' → 'job1'."""
    return (
        worker_id.removesuffix("_worker")
        if worker_id.endswith("_worker")
        else worker_id
    )


# Событие для graceful shutdown (устанавливается обработчиком сигналов)
_shutdown_event: asyncio.Event = asyncio.Event()

# Активные asyncio.Task воркеров: task_id → asyncio.Task
_running_tasks: dict[str, asyncio.Task] = {}


# ── Heartbeat ────────────────────────────────────────────────────────────────


async def heartbeat_writer(
    path: str = "data/heartbeat.txt",
    interval: int = 60,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Писать timestamp в файл каждые interval секунд (Docker HEALTHCHECK)."""
    ev = shutdown_event or _shutdown_event
    hb_path = Path(path)
    hb_path.parent.mkdir(parents=True, exist_ok=True)

    while not ev.is_set():
        try:
            hb_path.write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            logger.warning("heartbeat_writer: write failed: %s", exc)

        try:
            await asyncio.wait_for(ev.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ── Polling loops ─────────────────────────────────────────────────────────────


async def telegram_polling_loop(
    handler: TelegramHandler,
    interval: int = 30,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Цикл Telegram polling: poll_once каждые interval секунд."""
    ev = shutdown_event or _shutdown_event

    while not ev.is_set():
        try:
            events = await handler.poll_once()
            if events:
                logger.info("telegram: processed %d event(s)", len(events))
        except Exception as exc:
            logger.error("telegram_polling_loop: %s", exc)

        try:
            await asyncio.wait_for(ev.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def email_polling_loop(
    handler: EmailHandler,
    interval: int = 60,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Цикл Email polling (imaplib синхронный — запускаем в executor)."""
    ev = shutdown_event or _shutdown_event
    loop = asyncio.get_event_loop()

    while not ev.is_set():
        try:
            events = await loop.run_in_executor(None, handler.poll_once)
            if events:
                logger.info("email: processed %d event(s)", len(events))
        except Exception as exc:
            logger.error("email_polling_loop: %s", exc)

        try:
            await asyncio.wait_for(ev.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ── Dispatch ──────────────────────────────────────────────────────────────────


async def dispatch_pending_tasks(
    config: dict,
    tg_handler: TelegramHandler,
    db_path: Optional[str] = None,
    interval: int = 30,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Брать pending задачи из DB и запускать run_worker_cycle с ограничением concurrency.

    max_concurrent задаётся в supervisor.max_concurrent конфига.
    """
    ev = shutdown_event or _shutdown_event
    supervisor_cfg = config.get("supervisor", {})
    max_concurrent = int(supervisor_cfg.get("max_concurrent", 2))

    while not ev.is_set():
        try:
            # Очищаем завершённые tasks
            done_ids = [tid for tid, t in _running_tasks.items() if t.done()]
            for tid in done_ids:
                del _running_tasks[tid]

            slots_free = max_concurrent - len(_running_tasks)
            if slots_free > 0:
                with get_conn(db_path) as conn:
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE status='pending' ORDER BY created_at LIMIT ?",
                        (slots_free,),
                    ).fetchall()

                for row in rows:
                    task = dict(row)
                    task_id = task["id"]
                    if task_id not in _running_tasks:
                        t = asyncio.create_task(
                            run_worker_cycle(task, config, tg_handler, db_path),
                            name=f"worker_{task_id[:8]}",
                        )
                        _running_tasks[task_id] = t
                        logger.info("dispatch: started task_id=%s", task_id)

        except Exception as exc:
            logger.error("dispatch_pending_tasks: %s", exc)

        try:
            await asyncio.wait_for(ev.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ── Worker cycle ──────────────────────────────────────────────────────────────


def _build_worker_prompt(
    description: str,
    task_id: str = "",
    repos_context: Optional[list[dict]] = None,
    branch: str = "",
) -> str:
    """
    Собрать полный промпт для воркера: задание + workspace context + JSON-вывод.

    repos_context: [{"alias": "api", "path": "workspace/{task_id}/api"}]
    """
    # Контекст рабочей директории
    workspace_section = ""
    if repos_context:
        lines = [f"Task ID: {task_id}", f"Ветка: {branch}", "Репозитории:"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        lines.append("Работай ТОЛЬКО в этих директориях. Не выходи за их пределы.")
        workspace_section = "\n".join(lines) + "\n\n"

    return f"""\
Задание от супервайзора:

{description}

─────────────────────────────────────────────
{workspace_section}ОБЯЗАТЕЛЬНО: после выполнения задания выведи результат СТРОГО в этом формате
(без лишнего текста после <<<END>>>):

<<<JSON>>>
{{
  "status": "done",
  "confidence": <целое число 0-100>,
  "result": {{
    "repos": [{", ".join(f'{{"alias": "{r["alias"]}", "changed_files": [...], "entrypoint": null}}' for r in (repos_context or []))}],
    "notes": "<что именно сделано, одна-две строки>"
  }},
  "question": null
}}
<<<END>>>

Если задание непонятно или нужно уточнение — используй status "blocked" и заполни "question".
Если произошла ошибка — используй status "error" и опиши её в "notes".
confidence — твоя уверенность в правильности результата (0–100).
─────────────────────────────────────────────
"""


def _build_json_correction_prompt(previous_output: str) -> str:
    """
    Коррекционный промпт: показать что вышло и попросить JSON.

    Используется на повторных попытках когда воркер не вывел JSON-маркеры.
    """
    truncated = previous_output[:800] if len(previous_output) > 800 else previous_output
    return f"""\
В предыдущем ответе ты не вывел JSON в обязательном формате.

Твой предыдущий ответ:
{truncated}

Выведи результат ТОЛЬКО в этом формате (без лишнего текста):

<<<JSON>>>
{{
  "status": "done",
  "confidence": <0-100>,
  "result": {{
    "repos": [],
    "notes": "<что ты сделал>"
  }},
  "question": null
}}
<<<END>>>

Если задание не выполнено — используй status "blocked" (с "question") или "error".
"""


def _set_error_reason(task_id: str, reason: str, db_path: Optional[str] = None) -> None:
    """Записать last_error_reason в tasks для задачи."""
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE tasks SET last_error_reason=? WHERE id=?",
                (reason, task_id),
            )
    except Exception as _e:
        logger.warning("_set_error_reason: could not set error reason: %s", _e)


def _fail_final(
    task_id: str,
    worker_id: str,
    token: str,
    reason: str,
    db_path: Optional[str] = None,
) -> None:
    """Финальный сбой после всех попыток -> requires_manual."""
    _set_error_reason(task_id, reason, db_path)
    release_lease(task_id, worker_id, token, "requires_manual", db_path=db_path)
    logger.error(
        "run_worker_cycle: requires_manual task_id=%s reason=%s",
        task_id,
        reason,
    )


def _setup_worktrees(
    task_id: str,
    job: str,
    repos: list[dict],
    branch_pattern: str,
    base_branch: str,
    repo_mgr: RepoManager,
) -> list[str]:
    """
    Подготовить worktrees для каждого repo.

    Returns: list[str] — worktree aliases, которые были успешно созданы.
    Raises: Exception при ошибке (caller обработает).
    """
    worktree_aliases: list[str] = []
    for repo in repos:
        alias = repo["alias"]
        url = repo["url"]
        token_env = repo.get("token_env", "")
        git_token = os.getenv(token_env) if token_env else None

        repo_mgr.ensure_mirror(
            job,
            alias,
            url,
            clone_strategy=repo.get("clone_strategy", "mirror"),
            token=git_token,
        )

        branch = branch_pattern.replace("{task_id}", task_id)
        repo_mgr.prepare_worktree(task_id, job, alias, branch, base_branch)
        worktree_aliases.append(alias)

    logger.info(
        "run_worker_cycle: worktrees ready task_id=%s repos=%s",
        task_id,
        [r["alias"] for r in repos],
    )
    return worktree_aliases


async def _notify_failure(
    tg_handler: TelegramHandler, task_id: str, message: str
) -> None:
    """Async helper: уведомить владельца об ошибке задачи."""
    await tg_handler.notify_owner(
        f"\u26a0\ufe0f task#{task_id[:8]}: {message}\nПовтори: /retry {task_id[:8]}"
    )


def _build_reviewer_prompt(
    task_description: str,
    worker_result: dict,
    repos_context: Optional[list[dict]] = None,
) -> str:
    """Промпт для ревьюера: задание + результат воркера + JSON формат."""
    notes = worker_result.get("notes", "")
    repos_info = worker_result.get("repos", [])
    changed_files_section = ""
    for repo in repos_info:
        alias = repo.get("alias", "?")
        files = repo.get("changed_files", [])
        if files:
            changed_files_section += f"\n  {alias}: {', '.join(str(f) for f in files)}"

    workspace_section = ""
    if repos_context:
        lines = ["Репозитории:"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        workspace_section = "\n".join(lines) + "\n\n"

    return f"""\
Ты — ревьюер. Проверь результат воркера по заданию.

─────────────────────────────────────────────
Задание:
{task_description}

─────────────────────────────────────────────
Результат воркера:
{notes}

Изменённые файлы:{changed_files_section if changed_files_section else " (нет)"}

{workspace_section}─────────────────────────────────────────────
Проверь:
1. Код соответствует заданию
2. Нет явных ошибок, уязвимостей, нарушений стиля
3. Тесты покрывают основные сценарии

Выведи результат СТРОГО в этом формате:

<<<JSON>>>
{{
  "verdict": "APPROVED" или "NEEDS_CHANGES",
  "feedback": "<общий комментарий>",
  "issues": [
    {{"repo": "<alias>", "file": "<путь>", "line": null, "type": "bug|style|logic|test", "message": "<описание>"}}
  ]
}}
<<<END>>>

Если всё ОК — verdict "APPROVED" и пустой issues.
Если есть замечания — verdict "NEEDS_CHANGES" и заполни issues.
"""


def _build_worker_retry_prompt(
    task_description: str,
    attempt: int,
    max_attempts: int,
    prev_notes: str,
    reviewer_issues: list[dict],
    repos_context: Optional[list[dict]] = None,
    branch: str = "",
) -> str:
    """Промпт для повторной попытки воркера после NEEDS_CHANGES."""
    issues_lines = []
    for iss in reviewer_issues:
        repo = iss.get("repo", "?")
        file = iss.get("file", "?")
        line = iss.get("line")
        iss_type = iss.get("type", "?")
        msg = iss.get("message", "")
        loc = f"{file}:{line}" if line else file
        issues_lines.append(f"  - {repo}/{loc} [{iss_type}] {msg}")

    issues_text = (
        "\n".join(issues_lines) if issues_lines else "  (нет конкретных замечаний)"
    )

    workspace_section = ""
    if repos_context:
        lines = [f"Ветка: {branch}", "Репозитории:"]
        for repo in repos_context:
            lines.append(f"  - {repo['alias']}: {repo['path']}")
        lines.append("Работай ТОЛЬКО в этих директориях.")
        workspace_section = "\n".join(lines) + "\n\n"

    return f"""\
Задача: {task_description}
Попытка: {attempt}/{max_attempts}

Предыдущий результат: {prev_notes}

Замечания ревьюера:
{issues_text}

Исправь только указанные замечания. Не трогай то, что уже работает.

─────────────────────────────────────────────
{workspace_section}ОБЯЗАТЕЛЬНО: после выполнения выведи результат СТРОГО в этом формате:

<<<JSON>>>
{{
  "status": "done",
  "confidence": <0-100>,
  "result": {{
    "repos": [{", ".join(f'{{"alias": "{r["alias"]}", "changed_files": [...], "entrypoint": null}}' for r in (repos_context or []))}],
    "notes": "<что именно исправлено>"
  }},
  "question": null
}}
<<<END>>>
"""


async def _run_ci_and_push(
    task_id: str,
    job: str,
    alias: str,
    worker_cfg: dict,
    repo_mgr: RepoManager,
    db_path: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Запустить style formatters, git commit, CI checks, git push.

    Returns:
        (success, error_message)
    """
    wt_path = str(repo_mgr._worktree_path(task_id, alias))

    # 1. Style policy — run formatters before commit
    style_policy = worker_cfg.get("style_policy", {})
    if style_policy.get("run_before_commit"):
        for fmt_cmd in style_policy.get("formatters", []):
            try:
                _out, _err, rc = safe_exec(fmt_cmd, cwd=wt_path, timeout=120)
                if rc != 0:
                    logger.warning(
                        "_run_ci_and_push: formatter %s failed task_id=%s rc=%d",
                        fmt_cmd[0],
                        task_id,
                        rc,
                    )
            except Exception as exc:
                logger.warning(
                    "_run_ci_and_push: formatter error task_id=%s: %s", task_id, exc
                )

    # 2. Git add + commit
    try:
        safe_exec(["git", "add", "."], cwd=wt_path, timeout=60)
        _out, _err, rc = safe_exec(
            ["git", "commit", "-m", f"ai: task {task_id[:8]} — auto-commit"],
            cwd=wt_path,
            timeout=60,
        )
        if rc != 0:
            # Nothing to commit is OK (rc=1 with "nothing to commit")
            if "nothing to commit" not in _out and "nothing to commit" not in _err:
                return False, f"git commit failed (rc={rc}): {_err[:200]}"
    except Exception as exc:
        return False, f"git commit error: {str(exc)[:200]}"

    # 3. CI policy — run checks before push
    ci_policy = worker_cfg.get("ci_policy", {})
    ci_required = ci_policy.get("required_pass", False)
    for ci_cmd in ci_policy.get("run_before_push", []):
        try:
            _out, _err, rc = safe_exec(ci_cmd, cwd=wt_path, timeout=300)
            if rc != 0 and ci_required:
                return False, f"CI failed ({ci_cmd[0]}): {_err[:200]}"
        except Exception as exc:
            if ci_required:
                return (
                    False,
                    f"CI error ({ci_cmd[0] if ci_cmd else '?'}): {str(exc)[:200]}",
                )

    # 4. Git push
    try:
        _out, _err, rc = safe_exec(
            ["git", "push", "origin", "HEAD"],
            cwd=wt_path,
            timeout=120,
        )
        if rc != 0:
            return False, f"git push failed (rc={rc}): {_err[:200]}"
    except Exception as exc:
        return False, f"git push error: {str(exc)[:200]}"

    logger.info("_run_ci_and_push: success task_id=%s", task_id)
    return True, ""


async def _run_review_cycle(
    task_id: str,
    worker_id: str,
    token: str,
    task_description: str,
    worker_result: dict,
    config: dict,
    tg_handler,
    db_path: Optional[str] = None,
    repos_context: Optional[list[dict]] = None,
    branch: str = "",
    worker_dir: str = "",
    worker_timeout: int = 1800,
) -> None:
    """
    Цикл review: reviewer проверяет → при NEEDS_CHANGES → worker retry → повтор.

    При APPROVED → CI + push → release_lease done → TG.
    При exhausted iterations → release_lease requires_manual → TG.
    """
    from supervisor.claude_runner import ClaudeRunnerError, run_claude
    from supervisor.json_guard import extract_json, validate_reviewer_schema
    from supervisor.json_guard import validate_worker_schema
    from supervisor.run_logger import log_run

    worker_cfg = config.get("workers", {}).get(worker_id, {})
    reviewer_id = worker_cfg.get("reviewer_id")

    if not reviewer_id:
        # No reviewer — skip review, do CI+push if repos exist, then done
        if repos_context:
            repos = worker_cfg.get("repos", [])
            job = _worker_to_job(worker_id)
            repo_mgr = RepoManager()
            for repo in repos:
                success, err = await _run_ci_and_push(
                    task_id, job, repo["alias"], worker_cfg, repo_mgr, db_path
                )
                if not success:
                    _fail_final(task_id, worker_id, token, E_GIT_PUSH_FAIL, db_path)
                    await _notify_failure(tg_handler, task_id, f"CI/push failed: {err}")
                    return

        release_lease(task_id, worker_id, token, "done", db_path=db_path)
        notes = worker_result.get("notes", "")
        await tg_handler.notify_owner(
            f"task#{task_id[:8]}: DONE (no reviewer)\n{notes[:200]}"
        )
        logger.info("_run_review_cycle: done (no reviewer) task_id=%s", task_id)
        return

    reviewer_dir = f"workers/{reviewer_id}"
    max_iterations = int(worker_cfg.get("max_review_iterations", 3))
    current_worker_result = worker_result

    for iteration in range(1, max_iterations + 1):
        logger.info(
            "_run_review_cycle: iteration %d/%d task_id=%s",
            iteration,
            max_iterations,
            task_id,
        )

        # Build reviewer prompt
        reviewer_prompt = _build_reviewer_prompt(
            task_description, current_worker_result, repos_context
        )

        # Run reviewer
        try:
            reviewer_stdout = await run_claude(
                reviewer_prompt, cwd=reviewer_dir, timeout=worker_timeout
            )
        except (ClaudeRunnerError, asyncio.TimeoutError) as exc:
            logger.error(
                "_run_review_cycle: reviewer error task_id=%s: %s", task_id, exc
            )
            _fail_final(task_id, worker_id, token, E_REVIEWER_CRASH, db_path)
            await _notify_failure(
                tg_handler, task_id, f"reviewer crashed: {str(exc)[:150]}"
            )
            return

        # Parse reviewer response
        reviewer_parsed = extract_json(reviewer_stdout)
        reviewer_valid, reviewer_err = (
            validate_reviewer_schema(reviewer_parsed)
            if reviewer_parsed
            else (False, "no JSON from reviewer")
        )

        log_run(
            task_id=task_id,
            phase="reviewer",
            stdout=reviewer_stdout,
            stderr="",
            parsed_json=reviewer_parsed,
            json_valid=reviewer_valid,
            worker_id=reviewer_id,
            db_path=db_path,
        )

        if not reviewer_valid:
            logger.error(
                "_run_review_cycle: invalid reviewer json task_id=%s err=%s",
                task_id,
                reviewer_err,
            )
            _fail_final(task_id, worker_id, token, E_JSON_SCHEMA, db_path)
            await _notify_failure(
                tg_handler, task_id, f"reviewer JSON invalid: {reviewer_err[:150]}"
            )
            return

        verdict = reviewer_parsed.get("verdict")

        if verdict == "APPROVED":
            # CI + push
            if repos_context:
                repos = worker_cfg.get("repos", [])
                job = _worker_to_job(worker_id)
                repo_mgr = RepoManager()
                for repo in repos:
                    success, err = await _run_ci_and_push(
                        task_id, job, repo["alias"], worker_cfg, repo_mgr, db_path
                    )
                    if not success:
                        _fail_final(task_id, worker_id, token, E_GIT_PUSH_FAIL, db_path)
                        await _notify_failure(
                            tg_handler, task_id, f"CI/push failed: {err}"
                        )
                        return

            release_lease(task_id, worker_id, token, "done", db_path=db_path)
            feedback = reviewer_parsed.get("feedback", "")
            await tg_handler.notify_owner(
                f"task#{task_id[:8]}: DONE (reviewer APPROVED, iter={iteration})\n"
                f"{feedback[:200]}"
            )
            logger.info(
                "_run_review_cycle: approved task_id=%s iteration=%d",
                task_id,
                iteration,
            )
            return

        # NEEDS_CHANGES — retry worker
        is_last = iteration == max_iterations
        if is_last:
            _fail_final(task_id, worker_id, token, E_REVIEW_EXHAUSTED, db_path)
            feedback = reviewer_parsed.get("feedback", "")
            await tg_handler.notify_owner(
                f"task#{task_id[:8]}: review exhausted ({max_iterations} iterations).\n"
                f"{feedback[:200]}\n"
                f"Повтори: /retry {task_id[:8]}"
            )
            logger.warning(
                "_run_review_cycle: exhausted task_id=%s iterations=%d",
                task_id,
                max_iterations,
            )
            return

        # Build worker retry prompt and re-run worker
        prev_notes = current_worker_result.get("notes", "")
        issues = reviewer_parsed.get("issues", [])
        retry_prompt = _build_worker_retry_prompt(
            task_description,
            attempt=iteration + 1,
            max_attempts=max_iterations,
            prev_notes=prev_notes,
            reviewer_issues=issues,
            repos_context=repos_context,
            branch=branch,
        )

        try:
            worker_stdout = await run_claude(
                retry_prompt, cwd=worker_dir, timeout=worker_timeout
            )
        except (ClaudeRunnerError, asyncio.TimeoutError) as exc:
            logger.error(
                "_run_review_cycle: worker retry error task_id=%s: %s", task_id, exc
            )
            _fail_final(task_id, worker_id, token, E_WORKER_CRASH, db_path)
            await _notify_failure(
                tg_handler, task_id, f"worker retry crashed: {str(exc)[:150]}"
            )
            return

        # Parse worker retry result
        worker_parsed = extract_json(worker_stdout)
        worker_valid, worker_err = (
            validate_worker_schema(worker_parsed)
            if worker_parsed
            else (False, "no JSON from worker retry")
        )

        log_run(
            task_id=task_id,
            phase="worker",
            stdout=worker_stdout,
            stderr="",
            parsed_json=worker_parsed,
            json_valid=worker_valid,
            worker_id=worker_id,
            db_path=db_path,
        )

        if not worker_valid:
            logger.error(
                "_run_review_cycle: worker retry invalid json task_id=%s err=%s",
                task_id,
                worker_err,
            )
            _fail_final(task_id, worker_id, token, E_JSON_SCHEMA, db_path)
            await _notify_failure(
                tg_handler, task_id, f"worker retry JSON invalid: {worker_err[:150]}"
            )
            return

        # Update current result for next review iteration
        current_worker_result = worker_parsed.get("result", {})


async def _handle_worker_result(
    parsed: dict,
    task_id: str,
    worker_id: str,
    token: str,
    conf_threshold: int,
    attempt: int,
    max_attempts: int,
    tg_handler: TelegramHandler,
    db_path: Optional[str] = None,
    config: Optional[dict] = None,
    repos_context: Optional[list[dict]] = None,
    branch: str = "",
    worker_dir: str = "",
    worker_timeout: int = 1800,
) -> None:
    """Обработать валидный JSON результат воркера: done/blocked/error."""
    worker_status = parsed.get("status", "error")
    confidence = parsed.get("confidence", 0)
    attempt_note = f" (попытка {attempt}/{max_attempts})" if attempt > 1 else ""

    if worker_status == "done" and confidence >= conf_threshold:
        # Check if reviewer is configured
        effective_config = config or {}
        worker_cfg = effective_config.get("workers", {}).get(worker_id, {})
        reviewer_id = worker_cfg.get("reviewer_id")

        if reviewer_id:
            # Delegate to review cycle
            worker_result = parsed.get("result", {})
            task_description = parsed.get("_task_description", "")
            # _task_description is injected by run_worker_cycle before calling us
            await _run_review_cycle(
                task_id=task_id,
                worker_id=worker_id,
                token=token,
                task_description=task_description,
                worker_result=worker_result,
                config=effective_config,
                tg_handler=tg_handler,
                db_path=db_path,
                repos_context=repos_context,
                branch=branch,
                worker_dir=worker_dir,
                worker_timeout=worker_timeout,
            )
            return

        release_lease(task_id, worker_id, token, "done", db_path=db_path)
        notes = parsed.get("result", {}).get("notes", "")
        await tg_handler.notify_owner(
            f"task#{task_id[:8]}: DONE \u2713 (confidence={confidence}){attempt_note}\n"
            f"{notes[:200]}"
        )
        logger.info("run_worker_cycle: done task_id=%s", task_id)

    elif worker_status == "blocked" or (
        worker_status == "done" and confidence < conf_threshold
    ):
        release_lease(task_id, worker_id, token, "blocked", db_path=db_path)
        question = (
            parsed.get("question") or f"confidence={confidence} < {conf_threshold}"
        )
        await tg_handler.notify_owner(
            f"task#{task_id[:8]}: BLOCKED{attempt_note}. {question}\n"
            f"Повтори: /retry {task_id[:8]}"
        )
        logger.warning("run_worker_cycle: blocked task_id=%s", task_id)

    else:  # "error" от воркера — exhausted, финальный сбой
        _fail_final(task_id, worker_id, token, E_WORKER_CRASH, db_path)
        await _notify_failure(
            tg_handler,
            task_id,
            f"воркер вернул error{attempt_note}.",
        )
        logger.error(
            "run_worker_cycle: worker_error task_id=%s status=%s",
            task_id,
            worker_status,
        )


async def run_worker_cycle(
    task: dict,
    config: dict,
    tg_handler: TelegramHandler,
    db_path: Optional[str] = None,
    repo_manager: Optional[RepoManager] = None,
) -> None:
    """
    Полный цикл выполнения задачи: lease -> worktrees -> попытки -> json -> notify -> cleanup.

    Worktree-стратегия (repos из agents.yaml):
      - ensure_mirror: клон/обновление bare mirror
      - prepare_worktree: изолированная копия + симлинк в workspace воркера
      - cleanup_worktree: удаление после завершения (в finally)

    Retry-стратегия (max_attempts из agents.yaml):
      - ClaudeRunnerError (crash): повтор с тем же промптом
      - json_invalid / json_schema_invalid: повтор с коррекционным промптом
      - safeexec_timeout: не ретраить (таймаут повторится)
    Reviewer cycle, Notion, escalation — Фаза 3+.
    """
    from supervisor.claude_runner import ClaudeRunnerError, run_claude
    from supervisor.json_guard import extract_json, validate_worker_schema
    from supervisor.run_logger import log_run

    task_id = task["id"]
    worker_id = task["assigned_worker"]
    worker_cfg = config.get("workers", {}).get(worker_id, {})
    lease_ttl = int(os.getenv("WORKER_LEASE_TTL_SECONDS", "300"))
    worker_timeout = int(os.getenv("WORKER_TIMEOUT_SECONDS", "1800"))
    max_attempts = int(worker_cfg.get("max_attempts", 3))
    retry_delay = int(os.getenv("WORKER_RETRY_DELAY_SECONDS", "30"))

    logger.info("run_worker_cycle: start task_id=%s worker=%s", task_id, worker_id)

    token = acquire_lease(task_id, worker_id, ttl=lease_ttl, db_path=db_path)
    if not token:
        logger.warning("run_worker_cycle: lease conflict task_id=%s", task_id)
        return

    worker_dir = str(Path("workers") / worker_id)
    last_stdout = ""
    use_correction = False

    # ── Worktree setup ─────────────────────────────────────────────────────
    repos = worker_cfg.get("repos", [])
    job = _worker_to_job(worker_id)
    branching = worker_cfg.get("branching_policy", {})
    branch_pattern = branching.get("pattern", "ai/task-{task_id}")
    base_branch = branching.get("base", "main")

    repo_mgr = repo_manager or RepoManager()
    worktree_aliases: list[str] = []

    try:
        if repos:
            try:
                worktree_aliases = _setup_worktrees(
                    task_id, job, repos, branch_pattern, base_branch, repo_mgr
                )
            except Exception as exc:
                logger.error(
                    "run_worker_cycle: worktree setup failed task_id=%s: %s",
                    task_id,
                    exc,
                )
                _fail_final(task_id, worker_id, token, E_WORKER_CRASH, db_path)
                await tg_handler.notify_owner(
                    f"\u26a0\ufe0f task#{task_id[:8]}: не удалось подготовить worktree.\n"
                    f"{str(exc)[:150]}"
                )
                return

        # ── Retry loop ─────────────────────────────────────────────────────
        conf_threshold = int(
            worker_cfg.get(
                "confidence_threshold",
                config.get("supervisor", {}).get("confidence_threshold", 70),
            )
        )

        for attempt in range(1, max_attempts + 1):
            is_last = attempt == max_attempts
            logger.info(
                "run_worker_cycle: attempt %d/%d task_id=%s",
                attempt,
                max_attempts,
                task_id,
            )

            # Промпт: json_invalid -> коррекционный, иначе полный
            if use_correction and last_stdout:
                prompt = _build_json_correction_prompt(last_stdout)
            else:
                # Контекст рабочих директорий для воркера
                repos_context = (
                    [
                        {
                            "alias": r["alias"],
                            "path": f"workspace/{task_id}/{r['alias']}",
                        }
                        for r in repos
                    ]
                    if repos
                    else None
                )
                branch = branch_pattern.replace("{task_id}", task_id) if repos else ""
                prompt = _build_worker_prompt(
                    task["description"],
                    task_id=task_id,
                    repos_context=repos_context,
                    branch=branch,
                )
            use_correction = False

            # Запустить claude CLI
            try:
                stdout = await run_claude(
                    prompt, cwd=worker_dir, timeout=worker_timeout
                )
                last_stdout = stdout
            except asyncio.TimeoutError:
                logger.error(
                    "run_worker_cycle: timeout attempt=%d task_id=%s",
                    attempt,
                    task_id,
                )
                _fail_final(task_id, worker_id, token, E_SAFEEXEC_TIMEOUT, db_path)
                await _notify_failure(tg_handler, task_id, "таймаут воркера.")
                return
            except ClaudeRunnerError as exc:
                logger.error(
                    "run_worker_cycle: claude error attempt=%d task_id=%s: %s",
                    attempt,
                    task_id,
                    exc,
                )
                if is_last:
                    _fail_final(task_id, worker_id, token, E_WORKER_CRASH, db_path)
                    await _notify_failure(
                        tg_handler,
                        task_id,
                        f"воркер упал {max_attempts}\u00d7 подряд.\n{str(exc)[:150]}",
                    )
                    return
                logger.warning(
                    "run_worker_cycle: crash attempt=%d, retry in %ds task_id=%s",
                    attempt,
                    retry_delay,
                    task_id,
                )
                await asyncio.sleep(retry_delay)
                continue

            # Парсим и валидируем JSON
            parsed = extract_json(stdout)
            valid, err = (
                validate_worker_schema(parsed) if parsed else (False, "no JSON")
            )

            log_run(
                task_id=task_id,
                phase="worker",
                stdout=stdout,
                stderr="",
                parsed_json=parsed,
                json_valid=valid,
                worker_id=worker_id,
                db_path=db_path,
            )

            if not valid:
                logger.warning(
                    "run_worker_cycle: invalid json attempt=%d/%d task_id=%s err=%s",
                    attempt,
                    max_attempts,
                    task_id,
                    err,
                )
                if is_last:
                    reason = E_JSON_INVALID if parsed is None else E_JSON_SCHEMA
                    _fail_final(task_id, worker_id, token, reason, db_path)
                    await _notify_failure(
                        tg_handler,
                        task_id,
                        f"воркер не дал JSON {max_attempts}\u00d7 ({err}).",
                    )
                    return
                use_correction = True
                logger.warning(
                    "run_worker_cycle: retrying with correction attempt=%d task_id=%s",
                    attempt,
                    task_id,
                )
                continue

            # JSON валидный — обработка результата
            # Inject task description for review cycle
            parsed["_task_description"] = task["description"]

            # Build repos_context and branch for review cycle
            _repos_ctx = (
                [
                    {
                        "alias": r["alias"],
                        "path": f"workspace/{task_id}/{r['alias']}",
                    }
                    for r in repos
                ]
                if repos
                else None
            )
            _branch = branch_pattern.replace("{task_id}", task_id) if repos else ""

            await _handle_worker_result(
                parsed,
                task_id,
                worker_id,
                token,
                conf_threshold,
                attempt,
                max_attempts,
                tg_handler,
                db_path,
                config=config,
                repos_context=_repos_ctx,
                branch=_branch,
                worker_dir=worker_dir,
                worker_timeout=worker_timeout,
            )
            return

    except Exception as exc:
        logger.error("run_worker_cycle: unexpected error task_id=%s: %s", task_id, exc)
        _set_error_reason(task_id, E_WORKER_CRASH, db_path)
        try:
            release_lease(task_id, worker_id, token, "requires_manual", db_path=db_path)
        except Exception:
            pass

    finally:
        for alias in worktree_aliases:
            try:
                repo_mgr.cleanup_worktree(task_id, job, alias)
            except Exception as cleanup_exc:
                logger.warning(
                    "run_worker_cycle: cleanup failed task_id=%s alias=%s: %s",
                    task_id,
                    alias,
                    cleanup_exc,
                )


# ── Scheduled tasks ───────────────────────────────────────────────────────────


async def schedule_at(
    hour_utc: int,
    coro_fn: Callable,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Запускать coro_fn каждый день в hour_utc UTC."""
    ev = shutdown_event or _shutdown_event

    while not ev.is_set():
        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)

        delay = (target - now).total_seconds()
        logger.debug("schedule_at(%d): next run in %.0fs", hour_utc, delay)

        try:
            await asyncio.wait_for(ev.wait(), timeout=delay)
            return  # shutdown
        except asyncio.TimeoutError:
            pass

        if not ev.is_set():
            try:
                await coro_fn()
            except Exception as exc:
                logger.error("schedule_at(%d): %s", hour_utc, exc)


async def schedule_periodic(
    hour_utc: int,
    every_days: int,
    coro_fn: Callable,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Запускать coro_fn каждые every_days дней в hour_utc UTC."""
    ev = shutdown_event or _shutdown_event
    last_run: Optional[datetime] = None

    while not ev.is_set():
        now = datetime.now(timezone.utc)

        days_since = (now - last_run).days if last_run else every_days
        should_run = days_since >= every_days and now.hour == hour_utc

        if should_run:
            last_run = now
            try:
                await coro_fn()
            except Exception as exc:
                logger.error("schedule_periodic(%d, %d): %s", hour_utc, every_days, exc)

        try:
            await asyncio.wait_for(ev.wait(), timeout=3600)  # проверяем раз в час
        except asyncio.TimeoutError:
            pass


# ── Housekeeping ──────────────────────────────────────────────────────────────


async def nightly_housekeeping(db_path: Optional[str] = None) -> None:
    """Ночная уборка: stale leases + старые worktrees."""
    logger.info("nightly_housekeeping: start")

    count = release_stale(db_path=db_path)
    if count > 0:
        logger.warning("nightly_housekeeping: released %d stale lease(s)", count)

    retention = int(os.getenv("WORKTREE_RETENTION_DAYS", "7"))
    repo_mgr = RepoManager()
    removed = repo_mgr.cleanup_old_worktrees(retention_days=retention)
    logger.info("nightly_housekeeping: removed %d old worktree(s)", removed)


# ── Stubs (Фаза 2) ────────────────────────────────────────────────────────────


async def run_daily_summary(tg_handler: TelegramHandler) -> None:
    """Ежедневный дайджест — stub, реализуется в Фазе 2."""
    logger.info("run_daily_summary: stub — Phase 2")


async def run_health_check(
    tg_handler: TelegramHandler,
    db_path: Optional[str] = None,
) -> None:
    """Health check — stub, реализуется в Фазе 2."""
    logger.info("run_health_check: stub — Phase 2")


# ── Signal handling ───────────────────────────────────────────────────────────


def _setup_signal_handlers(shutdown_event: Optional[asyncio.Event] = None) -> None:
    """Установить SIGTERM/SIGINT для graceful shutdown."""
    ev = shutdown_event or _shutdown_event

    def _handle(signum, frame):
        logger.info("Signal %d received — shutting down", signum)
        ev.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


# ── Entry point ───────────────────────────────────────────────────────────────


async def main() -> None:
    """
    Точка входа. Инициализирует всё и запускает asyncio задачи.

    Порядок: .env → config_validator → DB → signals → handlers → tasks → wait.
    """
    load_dotenv()

    # Настройка логирования
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    log_fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"

    logging.basicConfig(level=log_level, format=log_fmt)

    # Файловый лог — ротация 10 МБ × 5 файлов
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "supervisor.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(log_fmt))
    logging.getLogger().addHandler(file_handler)

    logger.info("Supervisor starting (PID=%d)", os.getpid())

    # 1. Валидация конфига — fail-fast
    config = load_and_validate()

    # 2. Инициализация БД
    db_path = os.getenv("DB_PATH", "data/orchestrator.db")
    init_db(db_path)
    with get_conn(db_path) as conn:
        apply_migrations(conn)
    logger.info("DB ready at %s", db_path)

    # 3. Сигналы
    _setup_signal_handlers()

    # 4. Telegram handler
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_owner = int(os.getenv("TELEGRAM_OWNER_CHAT_ID", "0"))
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))

    from supervisor.router import Router

    router = Router()

    tg_handler = TelegramHandler(
        token=tg_token,
        owner_chat_id=tg_owner,
        db_path=db_path,
        router=router,
    )
    await tg_handler.start()

    # 5. Email handler
    email_handler = EmailHandler(
        imap_server=os.getenv("IMAP_SERVER", "imap.gmail.com"),
        user=os.getenv("GMAIL_USER", ""),
        password=os.getenv("GMAIL_APP_PASSWORD", ""),
        db_path=db_path,
        router=router,
        port=int(os.getenv("IMAP_PORT", "993")),
    )

    # 6. Scheduled settings из конфига
    supervisor_cfg = config.get("supervisor", {})
    daily_hour = int(supervisor_cfg.get("daily_summary_hour_utc", 9))
    health_hour = int(supervisor_cfg.get("health_check_hour_utc", 3))
    health_days = int(supervisor_cfg.get("health_check_every_days", 1))

    # 7. Запуск asyncio задач
    tasks = [
        asyncio.create_task(
            heartbeat_writer(interval=60),
            name="heartbeat",
        ),
        asyncio.create_task(
            telegram_polling_loop(
                tg_handler, interval=1
            ),  # long polling — не ждём poll_interval
            name="tg_polling",
        ),
        asyncio.create_task(
            email_polling_loop(email_handler, interval=poll_interval),
            name="email_polling",
        ),
        asyncio.create_task(
            dispatch_pending_tasks(config, tg_handler, db_path, interval=poll_interval),
            name="dispatcher",
        ),
        asyncio.create_task(
            schedule_at(daily_hour, lambda: run_daily_summary(tg_handler)),
            name="daily_summary",
        ),
        asyncio.create_task(
            schedule_periodic(
                health_hour, health_days, lambda: run_health_check(tg_handler, db_path)
            ),
            name="health_check",
        ),
        asyncio.create_task(
            schedule_at(0, lambda: nightly_housekeeping(db_path)),
            name="nightly",
        ),
    ]

    logger.info("Supervisor ready (%d asyncio tasks)", len(tasks))

    # 8. Ожидаем shutdown
    await _shutdown_event.wait()

    logger.info("Shutdown: cancelling %d tasks", len(tasks))
    for t in tasks:
        t.cancel()

    # Даём задачам завершиться
    await asyncio.gather(*tasks, return_exceptions=True)

    # Освобождаем stale leases
    release_stale(db_path=db_path)

    await tg_handler.stop()
    logger.info("Supervisor stopped")


if __name__ == "__main__":
    asyncio.run(main())
