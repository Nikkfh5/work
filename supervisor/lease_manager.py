"""
supervisor/lease_manager.py — атомарный захват задачи + state machine.

Реализует lease-механизм для предотвращения двойного захвата задачи.
Все операции атомарны через один UPDATE с WHERE-условием.

Инварианты:
- acquire_lease: один атомарный UPDATE, rowcount=0 → задача уже занята
- renew/release: проверяют все три поля (task_id, locked_by, lease_token)
- Stale lease: locked_until < now() → другой воркер может захватить задачу

Коды ошибок:
    E_LEASE_STALE    = "lease_stale"     — наш lease уже не актуален
    E_LEASE_CONFLICT = "lease_conflict"  — другой воркер занял задачу
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from storage.db import get_conn

logger = logging.getLogger(__name__)

E_LEASE_STALE = "lease_stale"
E_LEASE_CONFLICT = "lease_conflict"

# Допустимые статусы для захвата задачи
ACQUIRABLE_STATUSES = ("pending", "blocked")

# Допустимые переходы state machine
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running"},
    "pending_approval": {"pending", "rejected"},
    "running": {"done", "blocked", "error", "requires_manual"},
    "blocked": {"running"},
    "requires_manual": {"running", "cancelled"},
    # terminal
    "done": set(),
    "cancelled": set(),
    "rejected": set(),
    "error": set(),
}


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_uuid() -> str:
    return str(uuid.uuid4())


def acquire_lease(
    task_id: str,
    worker_id: str,
    ttl: int = 300,
    db_path: Optional[str] = None,
    now_fn: Callable[[], datetime] = _default_now,
    uuid_fn: Callable[[], str] = _default_uuid,
) -> Optional[str]:
    """
    Атомарно захватить задачу для воркера.

    Выполняет один UPDATE с WHERE-условием:
    - статус в (pending, blocked)
    - locked_until IS NULL или уже истёк

    Args:
        task_id:   ID задачи
        worker_id: ID воркера
        ttl:       время жизни lease в секундах (default 300)
        db_path:   путь к БД (None → из env DB_PATH)
        now_fn:    функция текущего времени (для тестов)
        uuid_fn:   функция генерации UUID (для тестов)

    Returns:
        lease_token (строка) если захвачено успешно, None если задача занята.
    """
    token = uuid_fn()
    now = now_fn()
    locked_until_iso = _add_seconds(now, ttl)

    now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
    with get_conn(db_path) as conn:
        result = conn.execute(
            """
            UPDATE tasks
            SET status       = 'running',
                locked_by    = ?,
                locked_until = ?,
                lease_token  = ?,
                worker_attempt = worker_attempt + 1,
                updated_at   = datetime('now')
            WHERE id = ?
              AND (
                status IN ('pending', 'blocked')
                OR (status = 'running' AND locked_until < ?)
              )
            """,
            (worker_id, locked_until_iso, token, task_id, now_iso),
        )
        acquired = result.rowcount > 0

    if acquired:
        logger.info(
            "lease_acquired task_id=%s worker=%s token=%s ttl=%d",
            task_id,
            worker_id,
            token,
            ttl,
        )
        return token
    else:
        logger.warning(
            "lease_conflict task_id=%s worker=%s",
            task_id,
            worker_id,
        )
        return None


def renew_lease(
    task_id: str,
    worker_id: str,
    lease_token: str,
    ttl: int = 300,
    db_path: Optional[str] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> bool:
    """
    Продлить lease на ttl секунд от текущего момента.

    Проверяет все три поля: task_id, locked_by, lease_token.
    Если rowcount=0 — наш lease уже не актуален (истёк, другой воркер захватил).

    Returns:
        True если продлено, False если lease устарел.
    """
    now = now_fn()
    new_locked_until = _add_seconds(now, ttl)

    with get_conn(db_path) as conn:
        result = conn.execute(
            """
            UPDATE tasks
            SET locked_until = ?,
                updated_at   = datetime('now')
            WHERE id          = ?
              AND locked_by   = ?
              AND lease_token = ?
              AND status      = 'running'
            """,
            (new_locked_until, task_id, worker_id, lease_token),
        )
        renewed = result.rowcount > 0

    if renewed:
        logger.debug("lease_renewed task_id=%s worker=%s", task_id, worker_id)
    else:
        logger.warning(
            "lease_stale task_id=%s worker=%s token=%s",
            task_id,
            worker_id,
            lease_token,
        )
    return renewed


def release_lease(
    task_id: str,
    worker_id: str,
    lease_token: str,
    new_status: str,
    db_path: Optional[str] = None,
) -> bool:
    """
    Освободить lease и установить новый статус задачи.

    Проверяет все три поля (task_id, locked_by, lease_token).
    Если rowcount=0 — lease уже не наш, логируем WARNING, не паникуем.

    Args:
        task_id:    ID задачи
        worker_id:  ID воркера
        lease_token: токен lease
        new_status: новый статус задачи ('done', 'error', 'blocked', ...)
        db_path:    путь к БД

    Returns:
        True если освобождено, False если lease уже не актуален.
    """
    with get_conn(db_path) as conn:
        result = conn.execute(
            """
            UPDATE tasks
            SET status       = ?,
                locked_by    = NULL,
                locked_until = NULL,
                lease_token  = NULL,
                updated_at   = datetime('now')
            WHERE id          = ?
              AND locked_by   = ?
              AND lease_token = ?
            """,
            (new_status, task_id, worker_id, lease_token),
        )
        released = result.rowcount > 0

    if released:
        logger.info(
            "lease_released task_id=%s worker=%s new_status=%s",
            task_id,
            worker_id,
            new_status,
        )
    else:
        logger.warning(
            "lease_stale task_id=%s worker=%s token=%s — release ignored",
            task_id,
            worker_id,
            lease_token,
        )
    return released


def release_stale(
    db_path: Optional[str] = None,
    now_fn: Callable[[], datetime] = _default_now,
) -> int:
    """
    Освободить все задачи в статусе 'running' с истёкшим locked_until.

    Используется nightly housekeeping и health_monitor.

    Returns:
        Количество освобождённых задач.
    """
    now = now_fn()
    now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
    with get_conn(db_path) as conn:
        result = conn.execute(
            """
            UPDATE tasks
            SET status       = 'error',
                locked_by    = NULL,
                locked_until = NULL,
                lease_token  = NULL,
                last_error_reason = 'lease_stale',
                updated_at   = datetime('now')
            WHERE status = 'running'
              AND locked_until < ?
            """,
            (now_iso,),
        )
        count = result.rowcount

    if count > 0:
        logger.warning("release_stale: released %d stale lease(s)", count)
    return count


def check_transition(current: str, target: str) -> bool:
    """
    Проверить допустимость перехода статусов.

    Args:
        current: текущий статус
        target:  целевой статус

    Returns:
        True если переход допустим.
    """
    return target in VALID_TRANSITIONS.get(current, set())


def _add_seconds(dt: datetime, seconds: int) -> str:
    """Вернуть ISO-строку времени dt + seconds секунд."""
    from datetime import timedelta

    result = dt + timedelta(seconds=seconds)
    # SQLite принимает без timezone info
    return result.strftime("%Y-%m-%d %H:%M:%S")
