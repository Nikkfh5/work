"""
tests/test_lease_manager.py — тесты для supervisor/lease_manager.py

Запуск: pytest tests/test_lease_manager.py -v
"""

import pytest
from datetime import datetime, timezone, timedelta

from storage.db import create_task
from storage.db import get_conn
from supervisor.lease_manager import (
    acquire_lease,
    renew_lease,
    release_lease,
    release_stale,
    check_transition,
    is_lease_valid,
)


@pytest.fixture
def task_id(db_path):
    return create_task(
        source="telegram",
        source_contact="@test",
        assigned_worker="job1_worker",
        description="Test task",
        client_contact="@test",
    )


def make_now(offset_seconds: int = 0):
    """Фабрика now_fn со смещением."""
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def now_fn():
        return base + timedelta(seconds=offset_seconds)

    return now_fn


token_counter = 0


def make_uuid(token: str = "test-token-001"):
    def uuid_fn():
        return token

    return uuid_fn


# ── Тесты acquire_lease ──────────────────────────────────────────────────────


def test_acquire_lease_success(db_path, task_id):
    """Первый захват должен вернуть lease_token."""
    token = acquire_lease(
        task_id=task_id,
        worker_id="worker_1",
        ttl=300,
        db_path=db_path,
        now_fn=make_now(),
        uuid_fn=make_uuid("tok-001"),
    )
    assert token == "tok-001"


def test_acquire_sets_status_to_running(db_path, task_id):
    """После захвата статус должен стать 'running'."""
    acquire_lease(task_id=task_id, worker_id="w1", db_path=db_path)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, locked_by FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row["status"] == "running"
    assert row["locked_by"] == "w1"


def test_acquire_increments_worker_attempt(db_path, task_id):
    """worker_attempt должен инкрементироваться при каждом захвате."""
    with get_conn(db_path) as conn:
        initial = conn.execute(
            "SELECT worker_attempt FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]

    acquire_lease(task_id=task_id, worker_id="w1", db_path=db_path)
    # Освобождаем чтобы захватить снова
    token = acquire_lease.__wrapped__ if hasattr(acquire_lease, "__wrapped__") else None
    # Простой способ: читаем worker_attempt напрямую
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT worker_attempt FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row["worker_attempt"] == initial + 1


def test_second_acquire_returns_none(db_path, task_id):
    """Второй захват той же задачи должен вернуть None."""
    t1 = acquire_lease(
        task_id=task_id,
        worker_id="w1",
        db_path=db_path,
        now_fn=make_now(),
        uuid_fn=make_uuid("tok-001"),
    )
    t2 = acquire_lease(
        task_id=task_id,
        worker_id="w2",
        db_path=db_path,
        now_fn=make_now(),
        uuid_fn=make_uuid("tok-002"),
    )
    assert t1 == "tok-001"
    assert t2 is None


def test_stale_lease_can_be_acquired(db_path, task_id):
    """Истёкший lease можно захватить другим воркером."""
    # Захватываем с коротким TTL в прошлом
    acquire_lease(
        task_id=task_id,
        worker_id="w1",
        ttl=300,
        db_path=db_path,
        now_fn=make_now(-400),  # 400 секунд назад
        uuid_fn=make_uuid("tok-old"),
    )
    # Вручную обновляем locked_until на прошлое чтобы симулировать истечение
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET locked_until='2020-01-01 00:00:00' WHERE id=?", (task_id,)
        )
    # Второй воркер захватывает истёкший lease
    token = acquire_lease(
        task_id=task_id,
        worker_id="w2",
        ttl=300,
        db_path=db_path,
        now_fn=make_now(),
        uuid_fn=make_uuid("tok-new"),
    )
    assert token == "tok-new"


def test_acquire_blocked_task(db_path, task_id):
    """Задача в статусе 'blocked' тоже захватывается."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
    token = acquire_lease(task_id=task_id, worker_id="w1", db_path=db_path)
    assert token is not None


def test_done_task_cannot_be_acquired(db_path, task_id):
    """Задача в статусе 'done' не захватывается."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    token = acquire_lease(task_id=task_id, worker_id="w1", db_path=db_path)
    assert token is None


# ── Тесты renew_lease ────────────────────────────────────────────────────────


def test_renew_lease_success(db_path, task_id):
    """Продление lease должно вернуть True."""
    token = acquire_lease(
        task_id=task_id,
        worker_id="w1",
        db_path=db_path,
        uuid_fn=make_uuid("tok-001"),
    )
    result = renew_lease(
        task_id=task_id, worker_id="w1", lease_token="tok-001", db_path=db_path
    )
    assert result is True


def test_renew_wrong_token_returns_false(db_path, task_id):
    """Продление с неверным токеном возвращает False."""
    acquire_lease(
        task_id=task_id, worker_id="w1", db_path=db_path, uuid_fn=make_uuid("tok-001")
    )
    result = renew_lease(
        task_id=task_id, worker_id="w1", lease_token="wrong-token", db_path=db_path
    )
    assert result is False


def test_renew_wrong_worker_returns_false(db_path, task_id):
    """Продление с неверным worker_id возвращает False."""
    acquire_lease(
        task_id=task_id, worker_id="w1", db_path=db_path, uuid_fn=make_uuid("tok-001")
    )
    result = renew_lease(
        task_id=task_id, worker_id="w2", lease_token="tok-001", db_path=db_path
    )
    assert result is False


# ── Тесты release_lease ──────────────────────────────────────────────────────


def test_release_lease_success(db_path, task_id):
    """Правильное освобождение возвращает True и устанавливает статус."""
    acquire_lease(
        task_id=task_id, worker_id="w1", db_path=db_path, uuid_fn=make_uuid("tok-001")
    )
    result = release_lease(
        task_id=task_id,
        worker_id="w1",
        lease_token="tok-001",
        new_status="done",
        db_path=db_path,
    )
    assert result is True
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, locked_by, lease_token FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row["status"] == "done"
    assert row["locked_by"] is None
    assert row["lease_token"] is None


def test_release_wrong_token_returns_false(db_path, task_id):
    """Освобождение с неверным токеном возвращает False."""
    acquire_lease(
        task_id=task_id, worker_id="w1", db_path=db_path, uuid_fn=make_uuid("tok-001")
    )
    result = release_lease(
        task_id=task_id,
        worker_id="w1",
        lease_token="wrong",
        new_status="done",
        db_path=db_path,
    )
    assert result is False


# ── Тесты release_stale ──────────────────────────────────────────────────────


def test_release_stale_frees_expired_tasks(db_path, task_id):
    """release_stale освобождает задачи с истёкшим locked_until."""
    acquire_lease(task_id=task_id, worker_id="w1", db_path=db_path)
    # Устанавливаем expired locked_until
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET locked_until='2020-01-01 00:00:00' WHERE id=?", (task_id,)
        )
    count = release_stale(db_path=db_path, now_fn=make_now())
    assert count == 1
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, last_error_reason FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row["status"] == "error"
    assert row["last_error_reason"] == "lease_stale"


def test_release_stale_not_expired_untouched(db_path, task_id):
    """release_stale не трогает задачи с активным lease."""
    acquire_lease(task_id=task_id, worker_id="w1", ttl=3600, db_path=db_path)
    count = release_stale(db_path=db_path, now_fn=make_now())
    assert count == 0


# ── Тесты check_transition ───────────────────────────────────────────────────


def test_valid_transitions():
    assert check_transition("pending", "running") is True
    assert check_transition("running", "done") is True
    assert check_transition("running", "blocked") is True
    assert check_transition("blocked", "running") is True


def test_invalid_transitions():
    assert check_transition("done", "running") is False
    assert check_transition("done", "pending") is False
    assert check_transition("running", "pending") is False
    assert check_transition("cancelled", "running") is False


# ── Тесты is_lease_valid (BUG-017) ─────────────────────────────────────────


def test_is_lease_valid_active(db_path, task_id):
    """is_lease_valid returns True for active lease with matching token."""
    acquire_lease(
        task_id=task_id, worker_id="w1", db_path=db_path, uuid_fn=make_uuid("tok-v1")
    )
    assert is_lease_valid(task_id, "w1", "tok-v1", db_path=db_path) is True


def test_is_lease_valid_wrong_token(db_path, task_id):
    """is_lease_valid returns False for wrong token."""
    acquire_lease(
        task_id=task_id, worker_id="w1", db_path=db_path, uuid_fn=make_uuid("tok-v2")
    )
    assert is_lease_valid(task_id, "w1", "wrong", db_path=db_path) is False


def test_is_lease_valid_after_release_stale(db_path, task_id):
    """is_lease_valid returns False after release_stale clears lease."""
    acquire_lease(
        task_id=task_id, worker_id="w1", db_path=db_path, uuid_fn=make_uuid("tok-v3")
    )
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET locked_until='2020-01-01 00:00:00' WHERE id=?", (task_id,)
        )
    release_stale(db_path=db_path, now_fn=make_now())
    assert is_lease_valid(task_id, "w1", "tok-v3", db_path=db_path) is False


def test_is_lease_valid_nonexistent_task(db_path):
    """is_lease_valid returns False for non-existent task."""
    assert is_lease_valid("no-such-task", "w1", "tok", db_path=db_path) is False
