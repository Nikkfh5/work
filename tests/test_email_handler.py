"""
tests/test_email_handler.py — тесты для integrations/email_handler.py

Запуск: pytest tests/test_email_handler.py -v

Тесты используют временную БД и мок imaplib.IMAP4_SSL.
Все тесты синхронные (EmailHandler — синхронный класс).
"""

import imaplib
import pytest
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from unittest.mock import MagicMock, patch

from storage.db import get_conn
from integrations.email_handler import EmailHandler


# ── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_router():
    """Мок Router: owner@example.com → job1_worker."""
    router = MagicMock()
    router.resolve_worker.return_value = "job1_worker"
    return router


@pytest.fixture
def handler(db_path, mock_router):
    """EmailHandler с реальной БД и мок-роутером. Без реального IMAP."""
    return EmailHandler(
        imap_server="imap.example.com",
        user="bot@example.com",
        password="secret",
        db_path=db_path,
        router=mock_router,
        port=993,
    )


def make_raw_email(
    subject: str = "Test subject",
    body: str = "Test body",
    from_addr: str = "owner@example.com",
    message_id: str = "<test-123@example.com>",
) -> bytes:
    """Сформировать raw bytes email-сообщения для тестов."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "bot@example.com"
    msg["Message-ID"] = message_id
    return msg.as_bytes()


def make_multipart_email(
    subject: str = "Multipart test",
    text_body: str = "Plain text part",
    from_addr: str = "owner@example.com",
    message_id: str = "<multi-456@example.com>",
) -> bytes:
    """Сформировать multipart email."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "bot@example.com"
    msg["Message-ID"] = message_id
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText("<b>HTML part</b>", "html", "utf-8"))
    return msg.as_bytes()


# ── Тесты: parse_email ────────────────────────────────────────────────────────


def test_parse_email_simple(handler):
    """Простое письмо: subject, from, body, message_id парсятся."""
    raw = make_raw_email(
        subject="Напиши тест",
        body="Нужно покрыть функцию foo",
        from_addr="owner@example.com",
        message_id="<abc-123@example.com>",
    )
    parsed = handler._parse_email(raw)

    assert parsed is not None
    assert parsed["message_id"] == "<abc-123@example.com>"
    assert "Напиши тест" in parsed["subject"]
    assert "foo" in parsed["body"]
    assert "owner@example.com" in parsed["from"]


def test_parse_email_multipart(handler):
    """Multipart письмо: берём text/plain часть."""
    raw = make_multipart_email(
        subject="Multipart task",
        text_body="Plain body content",
        message_id="<mp-789@example.com>",
    )
    parsed = handler._parse_email(raw)

    assert parsed is not None
    assert "Plain body content" in parsed["body"]
    assert "HTML" not in parsed["body"]


def test_parse_email_encoded_subject(handler):
    """Заголовок Subject в base64/quoted-printable декодируется."""
    # RFC 2047 encoded: =?utf-8?b?...?=
    import base64

    subject_text = "Задача для воркера"
    encoded = base64.b64encode(subject_text.encode("utf-8")).decode("ascii")
    raw = make_raw_email(
        subject=f"=?utf-8?b?{encoded}?=",
        message_id="<enc-001@example.com>",
    )
    parsed = handler._parse_email(raw)

    assert parsed is not None
    assert "Задача" in parsed["subject"]


# ── Тесты: создание задачи ────────────────────────────────────────────────────


def test_create_task_happy_path(handler, db_path, mock_router):
    """Письмо от известного контакта → задача в DB."""
    parsed = {
        "message_id": "<happy-001@example.com>",
        "subject": "Fix auth bug",
        "from": "owner@example.com",
        "body": "The token is not being refreshed.",
    }
    task_id = handler._create_task(parsed)

    assert task_id is not None
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row is not None
    assert row["source"] == "email"
    assert row["source_contact"] == "owner@example.com"
    assert row["assigned_worker"] == "job1_worker"
    assert "Fix auth bug" in row["description"]


def test_create_task_unknown_contact_returns_none(handler, mock_router):
    """Неизвестный email → задача не создаётся, возвращает None."""
    mock_router.resolve_worker.side_effect = ValueError("unknown")
    parsed = {
        "message_id": "<unk-001@example.com>",
        "subject": "task",
        "from": "stranger@unknown.com",
        "body": "body",
    }
    task_id = handler._create_task(parsed)
    assert task_id is None


def test_create_task_no_router_returns_none(db_path):
    """Без router → задача не создаётся."""
    h = EmailHandler(
        imap_server="imap.example.com",
        user="bot@example.com",
        password="x",
        db_path=db_path,
        router=None,
    )
    parsed = {"message_id": "<x@x.com>", "subject": "s", "from": "x@x.com", "body": "b"}
    assert h._create_task(parsed) is None


def test_create_task_empty_from_returns_none(handler):
    """Пустой From → задача не создаётся."""
    parsed = {"message_id": "<x@x.com>", "subject": "s", "from": "", "body": "b"}
    assert handler._create_task(parsed) is None


# ── Тесты: идемпотентность ────────────────────────────────────────────────────


def test_dedup_same_message_id_no_duplicate(handler, db_path, mock_router):
    """Повтор с тем же Message-ID → задача создаётся только один раз."""
    message_id = "<dedup-001@example.com>"

    # Первый вызов
    parsed = {
        "message_id": message_id,
        "subject": "Первый раз",
        "from": "owner@example.com",
        "body": "body",
    }
    task_id1 = handler._create_task(parsed)
    handler._mark_processed(message_id, task_id1)

    # Второй вызов — must be skipped
    assert handler._is_processed(message_id) is True

    # В DB только одна задача
    with get_conn(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 1


def test_is_processed_returns_false_for_new(handler):
    """Новый Message-ID → _is_processed возвращает False."""
    assert handler._is_processed("<new-000@example.com>") is False


def test_mark_processed_is_idempotent(handler, db_path):
    """Двойной вызов _mark_processed → не падает, не дублирует."""
    message_id = "<idem-001@example.com>"
    handler._mark_processed(message_id, task_id=None)
    handler._mark_processed(message_id, task_id=None)  # второй раз

    with get_conn(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM processed_emails WHERE message_id=?",
            (message_id,),
        ).fetchone()[0]
    assert count == 1


# ── Тесты: poll_once с мок IMAP ──────────────────────────────────────────────


def test_poll_once_processes_new_email(handler, db_path, mock_router):
    """poll_once с одним письмом → одна задача в DB."""
    raw = make_raw_email(
        subject="Новая задача",
        body="Написать юнит тесты",
        from_addr="owner@example.com",
        message_id="<poll-001@example.com>",
    )

    mock_imap = MagicMock()
    mock_imap.search.return_value = (None, [b"1"])
    mock_imap.fetch.return_value = (None, [(None, raw)])

    with patch.object(handler, "_connect") as mock_connect:
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_imap)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        results = handler.poll_once()

    assert len(results) == 1
    assert results[0]["message_id"] == "<poll-001@example.com>"

    with get_conn(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 1


def test_poll_once_skips_already_processed(handler, db_path):
    """poll_once с уже обработанным Message-ID → задача не дублируется."""
    message_id = "<already-001@example.com>"
    handler._mark_processed(message_id, task_id=None)

    raw = make_raw_email(message_id=message_id)

    mock_imap = MagicMock()
    mock_imap.search.return_value = (None, [b"1"])
    mock_imap.fetch.return_value = (None, [(None, raw)])

    with patch.object(handler, "_connect") as mock_connect:
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_imap)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        results = handler.poll_once()

    assert results == []


def test_poll_once_empty_inbox(handler):
    """Пустой INBOX → пустой список, без ошибок."""
    mock_imap = MagicMock()
    mock_imap.search.return_value = (None, [b""])

    with patch.object(handler, "_connect") as mock_connect:
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_imap)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        results = handler.poll_once()

    assert results == []


def test_poll_once_imap_error_returns_empty(handler):
    """Ошибка подключения к IMAP → пустой список, не падает."""
    with patch.object(handler, "_connect") as mock_connect:
        mock_connect.side_effect = imaplib.IMAP4.error("Connection refused")
        results = handler.poll_once()

    assert results == []


# ── Тесты: decode_header ─────────────────────────────────────────────────────


def test_decode_header_plain(handler):
    """Простой заголовок без кодирования."""
    result = handler._decode_header_value("Hello World")
    assert result == "Hello World"


def test_decode_header_empty(handler):
    """Пустой заголовок → пустая строка."""
    assert handler._decode_header_value("") == ""
    assert handler._decode_header_value(None) == ""
