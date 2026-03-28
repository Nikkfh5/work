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

Pipeline architecture:
  run_worker_cycle создаёт WorkerContext и прогоняет через stages:
    prepare_stage -> execute_stage -> review_stage -> deliver_stage
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
from supervisor.lease_manager import release_stale
from supervisor.pipeline import WorkerContext, _worker_to_job, run_pipeline
from supervisor.repo_manager import RepoManager
from supervisor.safe_exec import safe_exec  # noqa: F401 — tests patch this
from supervisor.stages.deliver import (
    _run_ci_and_push,  # noqa: F401 — re-export for tests
)
from supervisor.stages.execute import (
    _build_worker_prompt,  # noqa: F401 — re-export
)
from supervisor.stages.prepare import prepare_stage
from supervisor.stages.execute import execute_stage
from supervisor.stages.review import (
    _build_reviewer_prompt,  # noqa: F401 — re-export for tests
    _build_worker_retry_prompt,  # noqa: F401 — re-export for tests
    review_stage,
)
from supervisor.stages.deliver import deliver_stage

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
    max_backoff: int = 3600,
) -> None:
    """Цикл Email polling (imaplib синхронный — запускаем в executor).

    При ошибках — exponential backoff: interval → 2x → 4x → ... → max_backoff.
    При успехе — reset к базовому interval.
    """
    ev = shutdown_event or _shutdown_event
    loop = asyncio.get_event_loop()

    while not ev.is_set():
        try:
            events = await loop.run_in_executor(None, handler.poll_once)
            if events:
                logger.info("email: processed %d event(s)", len(events))
        except Exception as exc:
            logger.error("email_polling_loop: %s", exc)

        # Backoff based on handler's consecutive_errors
        errors = getattr(handler, "consecutive_errors", 0)
        wait = min(interval * (2 ** errors), max_backoff) if errors else interval
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
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
            # Release stale leases every dispatch cycle (audit fix #1)
            try:
                stale_count = release_stale(db_path=db_path)
                if stale_count > 0:
                    logger.warning("dispatch: released %d stale lease(s)", stale_count)
            except Exception as exc:
                logger.error("dispatch: release_stale error: %s", exc)

            # Очищаем завершённые tasks
            done_ids = [tid for tid, t in _running_tasks.items() if t.done()]
            for tid in done_ids:
                del _running_tasks[tid]

            slots_free = max_concurrent - len(_running_tasks)
            if slots_free > 0:
                with get_conn(db_path) as conn:
                    rows = conn.execute(
                        "SELECT id, status, assigned_worker, description, "
                        "title, priority, git_repo, source, source_contact, "
                        "client_contact, worker_attempt "
                        "FROM tasks WHERE status='pending' ORDER BY created_at LIMIT ?",
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


# ── Worker cycle (thin wrapper — delegates to pipeline) ──────────────────────


async def run_worker_cycle(
    task: dict,
    config: dict,
    tg_handler: TelegramHandler,
    db_path: Optional[str] = None,
    repo_manager: Optional[RepoManager] = None,
) -> None:
    """
    Полный цикл выполнения задачи через pipeline architecture.

    Создаёт WorkerContext и прогоняет через stages:
      prepare_stage -> execute_stage -> review_stage -> deliver_stage
    """
    worker_id = task["assigned_worker"]
    worker_cfg = config.get("workers", {}).get(worker_id, {})
    job = _worker_to_job(worker_id)
    branching = worker_cfg.get("branching_policy", {})

    ctx = WorkerContext(
        task_id=task["id"],
        worker_id=worker_id,
        task_description=task["description"],
        config=config,
        tg_handler=tg_handler,
        db_path=db_path,
        worker_cfg=worker_cfg,
        job=job,
        worker_dir=str(Path("workers") / worker_id),
        repos=worker_cfg.get("repos", []),
        branch_pattern=branching.get("pattern", "ai/task-{task_id}"),
        base_branch=branching.get("base", "main"),
        repo_manager=repo_manager or RepoManager(),
        lease_ttl=int(os.getenv("WORKER_LEASE_TTL_SECONDS", "300")),
        max_attempts=int(worker_cfg.get("max_attempts", 3)),
        worker_timeout=int(os.getenv("WORKER_TIMEOUT_SECONDS", "1800")),
        retry_delay=int(os.getenv("WORKER_RETRY_DELAY_SECONDS", "30")),
        conf_threshold=int(
            worker_cfg.get(
                "confidence_threshold",
                config.get("supervisor", {}).get("confidence_threshold", 70),
            )
        ),
    )

    logger.info(
        "run_worker_cycle: start task_id=%s worker=%s", ctx.task_id, ctx.worker_id
    )

    await run_pipeline(ctx, [prepare_stage, execute_stage, review_stage, deliver_stage])


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


# ── Imports (phase implementations) ───────────────────────────────────────────

from supervisor.summarizer import run_daily_summary  # noqa: E402


from supervisor.health_monitor import run_health_check  # noqa: E402


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
            schedule_at(daily_hour, lambda: run_daily_summary(tg_handler, db_path)),
            name="daily_summary",
        ),
        asyncio.create_task(
            schedule_periodic(
                health_hour,
                health_days,
                lambda: run_health_check(tg_handler, db_path, config),
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
