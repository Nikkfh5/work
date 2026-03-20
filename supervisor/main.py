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

logger = logging.getLogger(__name__)

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


def _build_worker_prompt(description: str) -> str:
    """
    Собрать полный промпт для воркера: задание + обязательный JSON-вывод.

    Инструкция по формату идёт в промпт (не только в CLAUDE.md), потому что
    claude --print выполняет одиночный запрос без интерактива — CLAUDE.md служит
    контекстом, но явная инструкция в промпте надёжнее.
    """
    return f"""\
Задание от супервайзора:

{description}

─────────────────────────────────────────────
ОБЯЗАТЕЛЬНО: после выполнения задания выведи результат СТРОГО в этом формате
(без лишнего текста после <<<END>>>):

<<<JSON>>>
{{
  "status": "done",
  "confidence": <целое число 0-100>,
  "result": {{
    "repos": [],
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


async def run_worker_cycle(
    task: dict,
    config: dict,
    tg_handler: TelegramHandler,
    db_path: Optional[str] = None,
    repo_manager: Optional[RepoManager] = None,
) -> None:
    """
    Полный цикл выполнения задачи: lease → worktrees → попытки → json → notify → cleanup.

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

    def _set_error_reason(reason: str) -> None:
        try:
            with get_conn(db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET last_error_reason=? WHERE id=?",
                    (reason, task_id),
                )
        except Exception as _e:
            logger.warning("run_worker_cycle: could not set error reason: %s", _e)

    def _fail_final(reason: str, message: str) -> None:
        """Финальный сбой после всех попыток → requires_manual + TG."""
        _set_error_reason(reason)
        release_lease(task_id, worker_id, token, "requires_manual", db_path=db_path)
        logger.error(
            "run_worker_cycle: requires_manual task_id=%s reason=%s",
            task_id,
            reason,
        )

    worker_dir = str(Path("workers") / worker_id)
    last_stdout = ""
    use_correction = (
        False  # True после json_invalid — использовать коррекционный промпт
    )

    # ── Worktree setup ─────────────────────────────────────────────────────
    repos = worker_cfg.get("repos", [])
    job = (
        worker_id.removesuffix("_worker")
        if worker_id.endswith("_worker")
        else worker_id
    )
    branching = worker_cfg.get("branching_policy", {})
    branch_pattern = branching.get("pattern", "ai/task-{task_id}")
    base_branch = branching.get("base", "main")

    repo_mgr = repo_manager or RepoManager()
    worktree_aliases: list[str] = []

    try:
        # Setup worktrees для каждого репо
        if repos:
            try:
                for repo in repos:
                    alias = repo["alias"]
                    url = repo["url"]
                    token_env = repo.get("token_env", "")
                    git_token = os.getenv(token_env, "") if token_env else None

                    repo_mgr.ensure_mirror(
                        job,
                        alias,
                        url,
                        clone_strategy=repo.get("clone_strategy", "mirror"),
                        token=git_token or None,
                    )

                    branch = branch_pattern.replace("{task_id}", task_id)
                    repo_mgr.prepare_worktree(task_id, job, alias, branch, base_branch)
                    worktree_aliases.append(alias)

                logger.info(
                    "run_worker_cycle: worktrees ready task_id=%s repos=%s",
                    task_id,
                    [r["alias"] for r in repos],
                )
            except Exception as exc:
                logger.error(
                    "run_worker_cycle: worktree setup failed task_id=%s: %s",
                    task_id,
                    exc,
                )
                _fail_final("worker_crash", str(exc))
                await tg_handler.notify_owner(
                    f"⚠️ task#{task_id[:8]}: не удалось подготовить worktree.\n"
                    f"{str(exc)[:150]}"
                )
                return  # finally cleanup will still run

        # ── Retry loop ─────────────────────────────────────────────────────
        for attempt in range(1, max_attempts + 1):
            is_last = attempt == max_attempts
            logger.info(
                "run_worker_cycle: attempt %d/%d task_id=%s",
                attempt,
                max_attempts,
                task_id,
            )

            # Промпт:
            #   - json_invalid на прошлой попытке → коррекционный (показать что вышло)
            #   - crash или первая попытка → полный промпт с заданием
            if use_correction and last_stdout:
                prompt = _build_json_correction_prompt(last_stdout)
            else:
                prompt = _build_worker_prompt(task["description"])
            use_correction = False  # сброс на каждой итерации

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
                _fail_final("safeexec_timeout", "")
                await tg_handler.notify_owner(
                    f"⚠️ task#{task_id[:8]}: таймаут воркера.\n"
                    f"Повтори: /retry {task_id[:8]}"
                )
                return  # таймаут — не ретраить
            except ClaudeRunnerError as exc:
                logger.error(
                    "run_worker_cycle: claude error attempt=%d task_id=%s: %s",
                    attempt,
                    task_id,
                    exc,
                )
                if is_last:
                    _fail_final("worker_crash", "")
                    await tg_handler.notify_owner(
                        f"⚠️ task#{task_id[:8]}: воркер упал {max_attempts}× подряд.\n"
                        f"{str(exc)[:150]}\n"
                        f"Повтори: /retry {task_id[:8]}"
                    )
                    return
                logger.warning(
                    "run_worker_cycle: crash attempt=%d, retry in %ds task_id=%s",
                    attempt,
                    retry_delay,
                    task_id,
                )
                await asyncio.sleep(retry_delay)
                # use_correction остаётся False → следующая попытка с полным промптом
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
                    reason = "json_invalid" if parsed is None else "json_schema_invalid"
                    _fail_final(reason, "")
                    await tg_handler.notify_owner(
                        f"⚠️ task#{task_id[:8]}: воркер не дал JSON {max_attempts}× "
                        f"({err}).\nПовтори: /retry {task_id[:8]}"
                    )
                    return
                use_correction = True  # следующая попытка — коррекционный промпт
                logger.warning(
                    "run_worker_cycle: retrying with correction attempt=%d task_id=%s",
                    attempt,
                    task_id,
                )
                continue

            # JSON валидный — обрабатываем статус воркера
            worker_status = parsed.get("status", "error")
            confidence = parsed.get("confidence", 0)
            conf_threshold = int(
                worker_cfg.get(
                    "confidence_threshold",
                    config.get("supervisor", {}).get("confidence_threshold", 70),
                )
            )
            attempt_note = f" (попытка {attempt}/{max_attempts})" if attempt > 1 else ""

            if worker_status == "done" and confidence >= conf_threshold:
                release_lease(task_id, worker_id, token, "done", db_path=db_path)
                notes = parsed.get("result", {}).get("notes", "")
                await tg_handler.notify_owner(
                    f"task#{task_id[:8]}: DONE ✓ (confidence={confidence}){attempt_note}\n"
                    f"{notes[:200]}"
                )
                logger.info("run_worker_cycle: done task_id=%s", task_id)

            elif worker_status == "blocked" or (
                worker_status == "done" and confidence < conf_threshold
            ):
                release_lease(task_id, worker_id, token, "blocked", db_path=db_path)
                question = (
                    parsed.get("question")
                    or f"confidence={confidence} < {conf_threshold}"
                )
                await tg_handler.notify_owner(
                    f"task#{task_id[:8]}: BLOCKED{attempt_note}. {question}\n"
                    f"Повтори: /retry {task_id[:8]}"
                )
                logger.warning("run_worker_cycle: blocked task_id=%s", task_id)

            else:  # "error" от воркера — exhausted, финальный сбой
                _fail_final("worker_crash", "")
                await tg_handler.notify_owner(
                    f"⚠️ task#{task_id[:8]}: воркер вернул error{attempt_note}.\n"
                    f"Повтори: /retry {task_id[:8]}"
                )
                logger.error(
                    "run_worker_cycle: worker_error task_id=%s status=%s",
                    task_id,
                    worker_status,
                )

            return  # цикл завершён (done / blocked / requires_manual)

    except Exception as exc:
        logger.error("run_worker_cycle: unexpected error task_id=%s: %s", task_id, exc)
        _set_error_reason("worker_crash")
        try:
            release_lease(task_id, worker_id, token, "requires_manual", db_path=db_path)
        except Exception:
            pass

    finally:
        # ── Worktree cleanup ───────────────────────────────────────────────
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
