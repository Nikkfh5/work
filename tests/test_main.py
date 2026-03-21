"""
tests/test_main.py — тесты для supervisor/main.py

Запуск: pytest tests/test_main.py -v

Тестируем изолированные функции (не main() целиком — требует реального TG токена).
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch

from storage.db import get_conn, create_task


# ── Тесты: heartbeat_writer ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_writer_creates_file(tmp_path):
    """heartbeat_writer создаёт файл с timestamp."""
    from supervisor.main import heartbeat_writer

    hb_path = tmp_path / "heartbeat.txt"
    ev = asyncio.Event()

    # Запускаем, ждём один тик, потом останавливаем
    task = asyncio.create_task(
        heartbeat_writer(path=str(hb_path), interval=0.05, shutdown_event=ev)
    )
    await asyncio.sleep(0.1)
    ev.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert hb_path.exists()
    content = hb_path.read_text()
    ts = float(content)
    assert abs(ts - time.time()) < 5  # в пределах 5 секунд


@pytest.mark.asyncio
async def test_heartbeat_writer_updates_file(tmp_path):
    """heartbeat_writer обновляет файл при каждом тике."""
    from supervisor.main import heartbeat_writer

    hb_path = tmp_path / "sub" / "heartbeat.txt"
    ev = asyncio.Event()

    # Запустить на очень малый интервал, потом остановить
    task = asyncio.create_task(
        heartbeat_writer(path=str(hb_path), interval=0.05, shutdown_event=ev)
    )
    await asyncio.sleep(0.15)
    ev.set()
    await task

    assert hb_path.exists()


# ── Тесты: schedule_at ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_at_calculates_next_run():
    """schedule_at вычисляет корректную задержку без зависания."""
    from supervisor.main import schedule_at

    calls = []

    async def coro():
        calls.append(time.time())

    ev = asyncio.Event()

    # Запускаем schedule_at, сразу устанавливаем shutdown
    task = asyncio.create_task(schedule_at(hour_utc=3, coro_fn=coro, shutdown_event=ev))
    # Немедленный shutdown — coro не должна вызваться
    ev.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert calls == []  # shutdown до срабатывания


# ── Тесты: dispatch_pending_tasks ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_picks_pending_task(db_path, mock_tg_handler):
    """dispatch_pending_tasks берёт задачу из DB со статусом pending."""
    from supervisor.main import dispatch_pending_tasks

    # Создаём pending задачу
    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="test task",
        client_contact="42",
    )

    config = {
        "supervisor": {"max_concurrent": 1},
        "workers": {"job1_worker": {}},
    }

    ev = asyncio.Event()
    started_task_ids = []

    # Мокаем run_worker_cycle чтобы не запускать реальный claude
    async def mock_cycle(task, config, tg_handler, db_path_arg):
        started_task_ids.append(task["id"])

    with patch("supervisor.main.run_worker_cycle", mock_cycle):
        task = asyncio.create_task(
            dispatch_pending_tasks(
                config, mock_tg_handler, db_path, interval=0.05, shutdown_event=ev
            )
        )
        await asyncio.sleep(0.15)
        ev.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert task_id in started_task_ids


@pytest.mark.asyncio
async def test_dispatch_respects_max_concurrent(db_path, mock_tg_handler):
    """dispatch_pending_tasks не запускает больше max_concurrent задач."""
    from supervisor.main import dispatch_pending_tasks

    # Создаём 3 задачи, max_concurrent=2
    for i in range(3):
        create_task(
            source="telegram",
            source_contact=f"@user{i}",
            assigned_worker="job1_worker",
            description=f"task {i}",
            client_contact="42",
        )

    config = {
        "supervisor": {"max_concurrent": 2},
        "workers": {"job1_worker": {}},
    }

    ev = asyncio.Event()
    started = []
    barrier = asyncio.Event()  # блокируем задачи до конца теста

    async def mock_cycle(task, config, tg_handler, db_path_arg):
        started.append(task["id"])
        await barrier.wait()  # держим задачу открытой

    with patch("supervisor.main.run_worker_cycle", mock_cycle):
        dispatch = asyncio.create_task(
            dispatch_pending_tasks(
                config, mock_tg_handler, db_path, interval=0.05, shutdown_event=ev
            )
        )
        await asyncio.sleep(0.2)

        # Должно быть не больше max_concurrent=2 запущенных
        assert len(started) <= 2

        barrier.set()
        ev.set()
        await asyncio.wait_for(dispatch, timeout=2.0)


# ── Тесты: telegram_polling_loop ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_polling_loop_calls_poll_once(mock_tg_handler):
    """telegram_polling_loop вызывает poll_once в цикле."""
    from supervisor.main import telegram_polling_loop

    ev = asyncio.Event()
    mock_tg_handler.poll_once = AsyncMock(return_value=[])

    task = asyncio.create_task(
        telegram_polling_loop(mock_tg_handler, interval=0.05, shutdown_event=ev)
    )
    await asyncio.sleep(0.2)
    ev.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert mock_tg_handler.poll_once.call_count >= 1


@pytest.mark.asyncio
async def test_telegram_polling_loop_continues_on_error(mock_tg_handler):
    """Ошибка в poll_once не останавливает polling loop."""
    from supervisor.main import telegram_polling_loop
    from telegram.error import TelegramError

    ev = asyncio.Event()
    call_count = 0

    async def poll_with_error():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TelegramError("Network error")
        return []

    mock_tg_handler.poll_once = poll_with_error

    task = asyncio.create_task(
        telegram_polling_loop(mock_tg_handler, interval=0.05, shutdown_event=ev)
    )
    await asyncio.sleep(0.2)
    ev.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert call_count >= 2  # продолжил работу после ошибки


# ── Тесты: nightly_housekeeping ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nightly_housekeeping_releases_stale(db_path):
    """nightly_housekeeping освобождает просроченные leases."""
    from supervisor.main import nightly_housekeeping
    from supervisor.lease_manager import acquire_lease

    # Создаём задачу и захватываем lease
    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="stale task",
        client_contact="42",
    )
    acquire_lease(task_id, "job1_worker", ttl=1, db_path=db_path)

    # Принудительно делаем lease просроченным
    import time as time_mod

    time_mod.sleep(2)

    await nightly_housekeeping(db_path=db_path)

    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    # Задача переведена в error (stale)
    assert row["status"] == "error"


# ── Тесты: run_worker_cycle ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_worker_cycle_lease_conflict(db_path, mock_tg_handler):
    """Если lease уже занят — run_worker_cycle завершается без уведомления."""
    from supervisor.main import run_worker_cycle
    from supervisor.lease_manager import acquire_lease

    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="conflict task",
        client_contact="42",
    )
    # Захватываем lease другим воркером
    acquire_lease(task_id, "other_worker", ttl=300, db_path=db_path)

    task = {"id": task_id, "assigned_worker": "job1_worker", "description": "test"}
    config = {"supervisor": {}, "workers": {"job1_worker": {}}}

    await run_worker_cycle(task, config, mock_tg_handler, db_path)

    # notify_owner не должен вызываться (просто молча вышли)
    mock_tg_handler.notify_owner.assert_not_called()


@pytest.mark.asyncio
async def test_run_worker_cycle_claude_error_notifies(db_path, mock_tg_handler):
    """Если claude падает — notify_owner вызывается с сообщением об ошибке."""
    from supervisor.main import run_worker_cycle
    from supervisor.claude_runner import ClaudeRunnerError

    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="claude error task",
        client_contact="42",
    )

    task = {"id": task_id, "assigned_worker": "job1_worker", "description": "test"}
    # max_attempts=1 чтобы не ждать WORKER_RETRY_DELAY_SECONDS между попытками
    config = {"supervisor": {}, "workers": {"job1_worker": {"max_attempts": 1}}}

    with patch(
        "supervisor.claude_runner.run_claude",
        AsyncMock(side_effect=ClaudeRunnerError("not found")),
    ):
        await run_worker_cycle(task, config, mock_tg_handler, db_path)

    mock_tg_handler.notify_owner.assert_called_once()
    call_arg = mock_tg_handler.notify_owner.call_args[0][0]
    # После исчерпания попыток → requires_manual + /retry инструкция
    assert "/retry" in call_arg


@pytest.mark.asyncio
async def test_run_worker_cycle_done_notifies(db_path, mock_tg_handler):
    """Воркер возвращает done с высоким confidence → notify_owner c DONE."""
    from supervisor.main import run_worker_cycle

    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="done task",
        client_contact="42",
    )

    task = {"id": task_id, "assigned_worker": "job1_worker", "description": "test"}
    config = {
        "supervisor": {"confidence_threshold": 70},
        "workers": {"job1_worker": {}},
    }

    worker_stdout = """
<<<JSON>>>
{
  "status": "done",
  "confidence": 90,
  "result": {"repos": [], "notes": "Всё готово"},
  "question": null
}
<<<END>>>
"""
    with patch(
        "supervisor.claude_runner.run_claude", AsyncMock(return_value=worker_stdout)
    ):
        await run_worker_cycle(task, config, mock_tg_handler, db_path)

    mock_tg_handler.notify_owner.assert_called_once()
    call_arg = mock_tg_handler.notify_owner.call_args[0][0]
    assert "DONE" in call_arg

    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "done"
