"""
tests/test_telegram_handler.py — тесты для integrations/telegram_handler.py

Запуск: pytest tests/test_telegram_handler.py -v

Все тесты используют временную БД (tmp_path) и моки telegram.Bot.
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from storage.db import get_conn
from integrations.telegram_handler import TelegramHandler


# ── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_router():
    """Мок Router: @testuser → job1_worker."""
    router = MagicMock()
    router.resolve_worker.return_value = "job1_worker"
    return router


@pytest.fixture
def handler(db_path, mock_router, tmp_path):
    """TelegramHandler с мок-ботом и реальной БД."""
    h = TelegramHandler(
        token="fake_token",
        owner_chat_id=12345,
        db_path=db_path,
        router=mock_router,
        logs_dir=str(tmp_path / "logs"),
    )
    # Вместо реального Bot — мок
    mock_bot = AsyncMock()
    mock_bot.get_updates = AsyncMock(return_value=[])
    mock_bot.send_message = AsyncMock()
    mock_bot.initialize = AsyncMock()
    mock_bot.shutdown = AsyncMock()
    h._bot = mock_bot
    return h


def make_update(
    update_id: int, text: str, username: str = "testuser", chat_id: int = 42
):
    """Создать мок telegram.Update."""
    update = MagicMock()
    update.update_id = update_id

    msg = MagicMock()
    msg.text = text
    msg.chat.id = chat_id
    msg.from_user.username = username

    update.message = msg
    return update


# ── Тесты: создание задачи из TG-сообщения ──────────────────────────────────


@pytest.mark.asyncio
async def test_poll_once_text_message_creates_task(handler, db_path, mock_router):
    """Свободный текст от известного контакта → задача в DB."""
    update = make_update(1001, "Напиши тест для функции foo")
    handler._bot.get_updates = AsyncMock(return_value=[update])

    events = await handler.poll_once()

    assert len(events) == 1
    assert events[0]["type"] == "task"
    task_id = events[0]["task_id"]

    # Проверяем что задача в DB
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row is not None
    assert row["source"] == "telegram"
    assert row["source_contact"] == "@testuser"
    assert row["assigned_worker"] == "job1_worker"
    assert "foo" in row["description"]


@pytest.mark.asyncio
async def test_poll_once_unknown_contact_no_task(handler, db_path):
    """Неизвестный контакт → задача НЕ создаётся, ответ отправлен."""
    handler._router.resolve_worker.side_effect = ValueError("unknown")

    update = make_update(1002, "Сделай задачу")
    handler._bot.get_updates = AsyncMock(return_value=[update])

    events = await handler.poll_once()

    assert events == []
    with get_conn(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 0


# ── Тесты: идемпотентность ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotency_same_update_id_no_duplicate(handler, db_path, mock_router):
    """Повторный poll с тем же update_id → дубликата задачи нет."""
    update = make_update(2001, "Первое сообщение")
    handler._bot.get_updates = AsyncMock(return_value=[update])

    # Первый poll
    events1 = await handler.poll_once()
    assert len(events1) == 1

    # Сбрасываем offset назад чтобы имитировать re-delivery
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES ('tg_last_update_id', '2000')"
        )

    # Второй poll с тем же update_id (уже в processed_tg_updates)
    events2 = await handler.poll_once()
    assert events2 == []

    with get_conn(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 1  # только одна задача


@pytest.mark.asyncio
async def test_offset_saved_after_poll(handler, db_path):
    """После poll offset сохраняется в kv_store."""
    update = make_update(3001, "Тест offset")
    handler._bot.get_updates = AsyncMock(return_value=[update])

    await handler.poll_once()

    offset = handler._get_offset()
    assert offset == 3001


# ── Тесты: команды ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_no_tasks(handler):
    """/status при пустой DB → сообщение 'нет задач'."""
    response = await handler._handle_status()
    assert "нет" in response.lower()


@pytest.mark.asyncio
async def test_status_with_active_tasks(handler, db_path):
    """/status с активными задачами → список."""
    from storage.db import create_task

    create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="Тестовая задача",
        client_contact="42",
    )

    response = await handler._handle_status()
    assert "job1_worker" in response
    assert "pending" in response


@pytest.mark.asyncio
async def test_approve_task(handler, db_path):
    """/approve переводит задачу pending_approval → pending."""
    from storage.db import create_task

    # Создаём задачу и вручную ставим статус pending_approval
    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="Health предлагает задачу",
        client_contact="42",
    )
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status='pending_approval' WHERE id=?", (task_id,)
        )

    response = await handler._handle_approve(task_id[:8], "Ок, делай")

    assert "одобрена" in response
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_reject_task(handler, db_path):
    """/reject переводит задачу pending_approval → rejected."""
    from storage.db import create_task

    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="Health предлагает задачу",
        client_contact="42",
    )
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status='pending_approval' WHERE id=?", (task_id,)
        )

    response = await handler._handle_reject(task_id[:8], "Не нужно")

    assert "отклонена" in response
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "rejected"


@pytest.mark.asyncio
async def test_approve_nonexistent_task(handler):
    """/approve несуществующей задачи → сообщение 'не найдена'."""
    response = await handler._handle_approve("deadbeef", "")
    assert "не найдена" in response


@pytest.mark.asyncio
async def test_approve_wrong_status(handler, db_path):
    """/approve задачи в статусе running → сообщение о неправильном статусе."""
    from storage.db import create_task

    task_id = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker="job1_worker",
        description="...",
        client_contact="42",
    )
    # Оставляем статус 'pending' (не pending_approval)

    response = await handler._handle_approve(task_id[:8], "")
    assert "не в статусе" in response


# ── Тесты: команды (диспетчер) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_command_routing(handler):
    """Неизвестная команда → подсказка с доступными командами."""
    response = await handler._handle_command(42, "/unknown", "@user")
    assert "/status" in response
    assert "/approve" in response


@pytest.mark.asyncio
async def test_poll_once_command_message(handler):
    """Команда /status в poll_once → event type=command, сообщение отправлено."""
    update = make_update(4001, "/status")
    handler._bot.get_updates = AsyncMock(return_value=[update])

    events = await handler.poll_once()

    assert len(events) == 1
    assert events[0]["type"] == "command"
    # Проверяем что send_message был вызван
    handler._bot.send_message.assert_called()


# ── Тесты: notify_owner и fallback ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_owner_success(handler):
    """notify_owner отправляет сообщение владельцу."""
    handler._bot.send_message = AsyncMock()

    result = await handler.notify_owner("task#abc: DONE ✓")

    assert result is True
    handler._bot.send_message.assert_called_once_with(
        chat_id=12345, text="task#abc: DONE ✓"
    )


@pytest.mark.asyncio
async def test_send_message_fallback_on_tg_error(handler, tmp_path):
    """При ошибке TG после retry → fallback в лог файл."""
    from telegram.error import TelegramError

    handler._bot.send_message = AsyncMock(side_effect=TelegramError("Network error"))

    result = await handler.send_message(12345, "Важное уведомление")

    assert result is False
    # Проверяем fallback файл
    fallback_files = list(Path(handler._logs_dir).glob("tg_fallback_*.log"))
    assert len(fallback_files) == 1
    content = fallback_files[0].read_text(encoding="utf-8")
    assert "12345" in content
    assert "Важное уведомление" in content


@pytest.mark.asyncio
async def test_send_message_truncates_long_text(handler):
    """Сообщение длиннее 4096 символов обрезается."""
    handler._bot.send_message = AsyncMock()
    long_text = "A" * 5000

    await handler.send_message(12345, long_text)

    called_text = handler._bot.send_message.call_args[1]["text"]
    assert len(called_text) <= 4096
    assert called_text.endswith("...")


# ── Тесты: обработка ошибок ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_once_get_updates_error_returns_empty(handler):
    """Ошибка get_updates → возвращает пустой список, не падает."""
    from telegram.error import TelegramError

    handler._bot.get_updates = AsyncMock(side_effect=TelegramError("Timeout"))

    events = await handler.poll_once()
    assert events == []


@pytest.mark.asyncio
async def test_poll_once_no_message_update_ignored(handler):
    """Update без message (например, edited_message) игнорируется."""
    update = MagicMock()
    update.update_id = 5001
    update.message = None  # нет message
    handler._bot.get_updates = AsyncMock(return_value=[update])

    events = await handler.poll_once()
    assert events == []
