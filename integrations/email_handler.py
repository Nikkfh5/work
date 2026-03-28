"""
integrations/email_handler.py — IMAP polling + Message-ID dedup + создание задач.

Инварианты:
- Идемпотентность: Message-ID каждого письма хранится в processed_emails
- Синхронный API (imaplib), вызывается через asyncio.run_in_executor
- НЕ запускает subprocess, НЕ пишет в task_runs
- Секреты (пароль) не попадают в логи (redact применяется при нужде)

Использование:
    handler = EmailHandler(imap_server="imap.gmail.com", user="bot@g.com",
                           password="app_password", router=router)
    events = handler.poll_once()  # список {"task_id": ..., "message_id": ...}
"""

import email
import email.message
import email.utils
import imaplib
import logging
from contextlib import contextmanager
from email.header import decode_header
from typing import Optional

from storage.db import create_task, get_conn

logger = logging.getLogger(__name__)

# Максимальный размер тела письма (символов)
_MAX_BODY_LEN = 10_000
# Максимальное количество писем за один poll
_MAX_EMAILS_PER_POLL = 20


class EmailHandler:
    """
    Обработчик Email: IMAP polling + Message-ID dedup + создание задач в DB.

    Синхронный класс — imaplib не поддерживает asyncio.
    В main.py запускается через loop.run_in_executor(None, handler.poll_once).
    """

    def __init__(
        self,
        imap_server: str,
        user: str,
        password: str,
        db_path: Optional[str] = None,
        router=None,
        port: int = 993,
    ):
        self._imap_server = imap_server
        self._user = user
        self._password = password
        self._port = port
        self._db_path = db_path
        self._router = router
        self.consecutive_errors: int = 0

    def poll_once(self) -> list[dict]:
        """
        Подключиться к IMAP, загрузить новые письма, создать задачи.

        Returns:
            Список {"task_id": str, "message_id": str} для каждого нового письма.
        """
        try:
            with self._connect() as imap:
                result = self._fetch_and_process(imap)
                self.consecutive_errors = 0
                return result
        except imaplib.IMAP4.error as exc:
            self.consecutive_errors += 1
            if self.consecutive_errors <= 3:
                logger.warning("email poll_once: IMAP error: %s", exc)
            else:
                logger.debug("email poll_once: IMAP error (x%d): %s", self.consecutive_errors, exc)
            return []
        except OSError as exc:
            self.consecutive_errors += 1
            if self.consecutive_errors <= 3:
                logger.warning("email poll_once: connection error: %s", exc)
            else:
                logger.debug("email poll_once: connection error (x%d): %s", self.consecutive_errors, exc)
            return []
        except Exception as exc:
            self.consecutive_errors += 1
            logger.error("email poll_once: unexpected error: %s", exc)
            return []

    @contextmanager
    def _connect(self):
        """Контекст-менеджер для IMAP4_SSL соединения."""
        imap = imaplib.IMAP4_SSL(self._imap_server, self._port)
        try:
            imap.login(self._user, self._password)
            imap.select("INBOX")
            yield imap
        finally:
            try:
                imap.close()
            except Exception:
                pass
            try:
                imap.logout()
            except Exception:
                pass

    def _fetch_and_process(self, imap: imaplib.IMAP4_SSL) -> list[dict]:
        """Найти непрочитанные письма и создать задачи."""
        _, data = imap.search(None, "UNSEEN")
        if not data or not data[0]:
            return []

        email_ids = data[0].split()
        # Ограничиваем количество за раз
        email_ids = email_ids[-_MAX_EMAILS_PER_POLL:]

        results = []
        for email_id in email_ids:
            try:
                _, msg_data = imap.fetch(email_id, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1]
                parsed = self._parse_email(raw)
                if not parsed:
                    continue

                message_id = parsed.get("message_id", "")
                if not message_id:
                    logger.warning("email: letter has no Message-ID, skipping")
                    continue

                if self._is_processed(message_id):
                    logger.debug("email: skip already processed message_id=%s", message_id[:40])
                    continue

                task_id = self._create_task(parsed)
                self._mark_processed(message_id, task_id)

                if task_id:
                    results.append({"task_id": task_id, "message_id": message_id})
                    logger.info(
                        "email: task_created task_id=%s message_id=%s",
                        task_id, message_id[:40],
                    )

            except Exception as exc:
                logger.error("email: error processing email_id=%s: %s", email_id, exc)

        return results

    def _parse_email(self, raw_bytes: bytes) -> Optional[dict]:
        """Разобрать raw байты письма в dict."""
        try:
            msg = email.message_from_bytes(raw_bytes)
            message_id = msg.get("Message-ID", "").strip()
            subject = self._decode_header_value(msg.get("Subject", ""))
            from_raw = msg.get("From", "")
            body = self._extract_body(msg)
            return {
                "message_id": message_id,
                "subject": subject,
                "from": from_raw,
                "body": body[:_MAX_BODY_LEN],
            }
        except Exception as exc:
            logger.error("email: parse error: %s", exc)
            return None

    def _decode_header_value(self, value: str) -> str:
        """Декодировать заголовок письма (может быть в base64/quoted-printable)."""
        if not value:
            return ""
        parts = decode_header(value)
        result = []
        for part, encoding in parts:
            if isinstance(part, bytes):
                result.append(part.decode(encoding or "utf-8", errors="replace"))
            else:
                result.append(str(part))
        return " ".join(result).strip()

    def _extract_body(self, msg: email.message.Message) -> str:
        """Извлечь text/plain тело письма."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return ""

    def _create_task(self, parsed: dict) -> Optional[str]:
        """Создать задачу в DB по данным письма."""
        from_raw = parsed.get("from", "")
        # Извлекаем чистый email из поля From: "Name <email@host>"
        _, email_addr = email.utils.parseaddr(from_raw)
        email_addr = email_addr.lower().strip()

        if not email_addr:
            logger.warning("email: cannot extract email address from From: %s", from_raw[:80])
            return None

        if not self._router:
            logger.warning("email: router not set, cannot route email from %s", email_addr)
            return None

        try:
            worker_id = self._router.resolve_worker("email", email_addr)
        except ValueError:
            logger.warning("email: unknown contact %s", email_addr)
            return None

        subject = parsed.get("subject", "")
        body = parsed.get("body", "")
        description = f"{subject}\n\n{body}".strip() if body else subject

        task_id = create_task(
            source="email",
            source_contact=email_addr,
            assigned_worker=worker_id,
            description=description,
            client_contact=email_addr,
            title=subject[:200] if subject else "",
        )
        return task_id

    def _is_processed(self, message_id: str) -> bool:
        """Проверить, обработано ли письмо с данным Message-ID."""
        try:
            with get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM processed_emails WHERE message_id=?",
                    (message_id,),
                ).fetchone()
            return row is not None
        except Exception as exc:
            logger.warning("email: is_processed check failed message_id=%s: %s",
                           message_id[:40], exc)
            return False

    def _mark_processed(self, message_id: str, task_id: Optional[str] = None) -> None:
        """Записать Message-ID в processed_emails (идемпотентно)."""
        try:
            with get_conn(self._db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO processed_emails (message_id, task_id) VALUES (?, ?)",
                    (message_id, task_id),
                )
        except Exception as exc:
            logger.error(
                "email: mark_processed failed message_id=%s: %s",
                message_id[:40], exc,
            )
