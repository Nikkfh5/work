"""
integrations/telegram_handler.py — Telegram polling + команды + уведомления.

Инварианты:
- Supervisor единственный кто вызывает notify_owner/send_message
- Идемпотентность: offset хранится в kv_store, processed_tg_updates — второй слой dedup
- Fallback если TG недоступен: лог в logs/tg_fallback_{date}.log + retry 3×
- НЕ запускает subprocess, НЕ пишет в task_runs

Команды владельца:
  /status                  → список активных задач
  /approve <id> [заметка]  → pending_approval → pending
  /reject <id> [причина]   → pending_approval → rejected
  /summary                 → stub (Фаза 2)
  свободный текст          → создать задачу через router
"""

import asyncio
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import Bot, Update
from telegram.error import TelegramError

from storage.db import create_task, get_conn
from supervisor.log_utils import redact

logger = logging.getLogger(__name__)

# Максимальная длина TG-сообщения
_TG_MAX_LEN = 4096
# Количество retry при ошибке отправки
_SEND_RETRIES = 3


class TelegramHandler:
    """
    Обработчик Telegram: polling updates, команды, отправка уведомлений.

    Использует ручной polling (Bot.get_updates) для интеграции в asyncio event loop.
    Идемпотентность обеспечивается двумя слоями:
      1. offset: запрашиваем только updates > last_offset
      2. processed_tg_updates: пропускаем уже обработанные update_id
    """

    def __init__(
        self,
        token: str,
        owner_chat_id: int | str,
        db_path: Optional[str] = None,
        router=None,
        logs_dir: str = "logs",
    ):
        self._token = token
        self._owner_chat_id = int(owner_chat_id)
        self._db_path = db_path
        self._router = router
        self._logs_dir = logs_dir
        self._bot: Optional[Bot] = None

    async def start(self) -> None:
        """Инициализировать Bot-сессию."""
        self._bot = Bot(self._token)
        await self._bot.initialize()
        logger.info("TelegramHandler started")

    async def stop(self) -> None:
        """Завершить Bot-сессию."""
        if self._bot:
            await self._bot.shutdown()
            logger.info("TelegramHandler stopped")

    async def poll_once(self) -> list[dict]:
        """
        Запросить одну пачку updates и обработать их.

        Returns:
            Список событий: {"type": "task"|"command", "update_id": int, ...}
        """
        assert self._bot is not None, "Call start() before poll_once()"

        offset = self._get_offset()
        try:
            updates: list[Update] = await self._bot.get_updates(
                offset=offset + 1,
                limit=100,
                timeout=30,  # long polling: Telegram держит соединение до 30s
            )
        except asyncio.TimeoutError:
            logger.debug("telegram poll_once: long polling timeout (normal)")
            return []
        except TelegramError as exc:
            logger.warning("telegram poll_once: get_updates error: %s", exc)
            return []
        except Exception as exc:
            logger.error("telegram poll_once: unexpected error: %s", exc)
            return []

        events = []
        for update in updates:
            # Второй слой dedup — пропускаем уже обработанные
            if self._is_processed(update.update_id):
                logger.debug("telegram: skip already processed update_id=%d", update.update_id)
                self._save_offset(update.update_id)
                continue

            event = await self._process_update(update)
            self._mark_processed(update.update_id, task_id=event.get("task_id") if event else None)
            self._save_offset(update.update_id)

            if event:
                events.append(event)

        return events

    async def notify_owner(self, text: str) -> bool:
        """Отправить уведомление владельцу системы."""
        return await self.send_message(self._owner_chat_id, text)

    async def send_message(self, chat_id: int | str, text: str) -> bool:
        """
        Отправить сообщение с retry и fallback в файл.

        Returns:
            True если отправлено, False если все попытки исчерпаны.
        """
        assert self._bot is not None, "Call start() before send_message()"

        # Обрезаем до лимита TG
        if len(text) > _TG_MAX_LEN:
            text = text[:_TG_MAX_LEN - 3] + "..."

        for attempt in range(1, _SEND_RETRIES + 1):
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
                return True
            except TelegramError as exc:
                logger.warning(
                    "telegram send_message attempt=%d chat_id=%s error: %s",
                    attempt, chat_id, exc,
                )

        logger.error("telegram send_message failed after %d attempts chat_id=%s", _SEND_RETRIES, chat_id)
        self._write_fallback_log(f"TO:{chat_id} {text}")
        return False

    # ── Обработка update ─────────────────────────────────────────────────────

    async def _process_update(self, update: Update) -> Optional[dict]:
        """Маршрутизировать update: команда или создание задачи."""
        if not update.message or not update.message.text:
            return None

        msg = update.message
        text = msg.text.strip()
        chat_id = msg.chat.id
        username = (
            f"@{msg.from_user.username}"
            if msg.from_user and msg.from_user.username
            else str(chat_id)
        )

        if text.startswith("/"):
            response = await self._handle_command(chat_id, text, username)
            await self.send_message(chat_id, response)
            return {"type": "command", "update_id": update.update_id, "cmd": text.split()[0]}
        else:
            return await self._create_task_from_message(chat_id, text, username, update.update_id)

    async def _handle_command(self, chat_id: int, text: str, username: str) -> str:
        """Диспетчер команд /status, /approve, /reject, /retry, /cancel, /summary."""
        parts = text.split(maxsplit=2)
        cmd = parts[0].lower().split("@")[0]  # убрать @botname если есть

        if cmd == "/status":
            return await self._handle_status()
        elif cmd == "/errors":
            return await self._handle_errors()
        elif cmd == "/approve" and len(parts) >= 2:
            note = parts[2] if len(parts) > 2 else ""
            return await self._handle_approve(parts[1], note)
        elif cmd == "/reject" and len(parts) >= 2:
            reason = parts[2] if len(parts) > 2 else ""
            return await self._handle_reject(parts[1], reason)
        elif cmd == "/retry" and len(parts) >= 2:
            return await self._handle_retry(parts[1])
        elif cmd == "/cancel" and len(parts) >= 2:
            return await self._handle_cancel(parts[1])
        elif cmd == "/plan" and len(parts) >= 2:
            return await self._handle_plan(parts[1])
        elif cmd == "/revise" and len(parts) >= 3:
            feedback = " ".join(parts[2:])
            return await self._handle_revise(parts[1], feedback)
        elif cmd == "/revise" and len(parts) == 2:
            return "Укажи фидбек: /revise <id> <комментарий>"
        elif cmd == "/summary":
            return "Дайджест: в разработке (Фаза 2)."
        else:
            return (
                f"Неизвестная команда: {cmd}\n"
                "Доступные:\n"
                "/status — активные задачи\n"
                "/errors — задачи с ошибками\n"
                "/retry <id> — повторить упавшую задачу\n"
                "/cancel <id> — отменить задачу\n"
                "/approve <id> — одобрить (задачу или план)\n"
                "/reject <id> — отклонить\n"
                "/plan <id> — посмотреть план\n"
                "/revise <id> <фидбек> — доработать план\n"
                "/summary — дайджест (Фаза 2)"
            )

    async def _handle_status(self) -> str:
        """Активные задачи + отдельно requires_manual (нужно действие) + счётчик ошибок."""
        try:
            with get_conn(self._db_path) as conn:
                active_rows = conn.execute(
                    """
                    SELECT id, status, assigned_worker, title, created_at
                    FROM tasks
                    WHERE status IN ('pending', 'pending_approval', 'running', 'blocked', 'planning', 'plan_review')
                    ORDER BY created_at DESC LIMIT 10
                    """,
                ).fetchall()
                stuck_rows = conn.execute(
                    """
                    SELECT id, assigned_worker, title, last_error_reason
                    FROM tasks WHERE status='requires_manual'
                    ORDER BY updated_at DESC LIMIT 5
                    """,
                ).fetchall()
                error_count = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='error'",
                ).fetchone()[0]

            lines = []
            if active_rows:
                lines.append("Активные задачи:")
                for r in active_rows:
                    short_id = r["id"][:8]
                    title = r["title"] or "—"
                    lines.append(f"• #{short_id} [{r['status']}] {r['assigned_worker']}: {title}")
            else:
                lines.append("Активных задач нет.")

            if stuck_rows:
                lines.append("\n🔴 Требуют действия (requires_manual):")
                for r in stuck_rows:
                    short_id = r["id"][:8]
                    reason = r["last_error_reason"] or "—"
                    lines.append(
                        f"• #{short_id} {r['assigned_worker']}: {reason}"
                        f"  /retry {short_id} | /cancel {short_id}"
                    )

            if error_count:
                lines.append(f"\n⚠️ Временных ошибок: {error_count} → /errors")

            return "\n".join(lines)
        except Exception as exc:
            logger.error("handle_status error: %s", exc)
            return "Ошибка получения статуса."

    async def _handle_approve(self, task_id_prefix: str, note: str) -> str:
        """Перевести задачу pending_approval/requires_manual → pending, или одобрить план."""
        full_id = self._resolve_task_id(task_id_prefix)
        if not full_id:
            return f"Задача {task_id_prefix!r} не найдена."

        try:
            with get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (full_id,)
                ).fetchone()
                if not row:
                    return f"Задача #{task_id_prefix[:8]} не найдена."

                status = row["status"]

                if status == "plan_review":
                    # Сигнализируем planning_stage через partial_result
                    conn.execute(
                        "UPDATE tasks SET partial_result='plan:approved', "
                        "updated_at=datetime('now') WHERE id=?",
                        (full_id,),
                    )
                    logger.info("plan_approved task_id=%s", full_id)
                    return f"✓ План #{task_id_prefix[:8]} одобрен. Запускаю выполнение."

                elif status in ("pending_approval", "requires_manual"):
                    result = conn.execute(
                        "UPDATE tasks SET status='pending', last_error_reason=NULL, "
                        "updated_at=datetime('now') "
                        "WHERE id=? AND status IN ('pending_approval', 'requires_manual')",
                        (full_id,),
                    )
                    if result.rowcount > 0:
                        logger.info(
                            "task_approved task_id=%s note=%s",
                            full_id, redact(note[:80]) if note else "",
                        )
                        return f"✓ Задача #{task_id_prefix[:8]} одобрена и поставлена в очередь."
                    return f"Задача #{task_id_prefix[:8]} не в статусе для одобрения."

                else:
                    return (
                        f"Задача #{task_id_prefix[:8]} не в статусе для одобрения "
                        f"(текущий: {status})."
                    )
        except Exception as exc:
            logger.error("handle_approve error task_id=%s: %s", full_id, exc)
            return "Ошибка при одобрении."

    async def _handle_reject(self, task_id_prefix: str, reason: str) -> str:
        """Перевести задачу pending_approval → rejected, или отклонить план."""
        full_id = self._resolve_task_id(task_id_prefix)
        if not full_id:
            return f"Задача {task_id_prefix!r} не найдена."

        try:
            with get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (full_id,)
                ).fetchone()
                if not row:
                    return f"Задача #{task_id_prefix[:8]} не найдена."

                status = row["status"]

                if status == "plan_review":
                    # Сигнализируем planning_stage через partial_result
                    conn.execute(
                        "UPDATE tasks SET partial_result='plan:rejected', "
                        "updated_at=datetime('now') WHERE id=?",
                        (full_id,),
                    )
                    logger.info("plan_rejected task_id=%s", full_id)
                    return f"✗ План #{task_id_prefix[:8]} отклонён. Задача будет отменена."

                elif status == "pending_approval":
                    result = conn.execute(
                        "UPDATE tasks SET status='rejected', updated_at=datetime('now') "
                        "WHERE id=? AND status='pending_approval'",
                        (full_id,),
                    )
                    if result.rowcount > 0:
                        logger.info(
                            "task_rejected task_id=%s reason=%s",
                            full_id, redact(reason[:80]) if reason else "",
                        )
                        return f"✗ Задача #{task_id_prefix[:8]} отклонена."
                    return f"Задача #{task_id_prefix[:8]} не в статусе pending_approval."

                else:
                    return (
                        f"Задача #{task_id_prefix[:8]} не в статусе для отклонения "
                        f"(текущий: {status})."
                    )
        except Exception as exc:
            logger.error("handle_reject error task_id=%s: %s", full_id, exc)
            return "Ошибка при отклонении."

    async def _handle_errors(self) -> str:
        """Список задач в статусе error и requires_manual."""
        try:
            with get_conn(self._db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT id, status, assigned_worker, title, last_error_reason, updated_at
                    FROM tasks WHERE status IN ('error', 'requires_manual')
                    ORDER BY status DESC, updated_at DESC LIMIT 15
                    """,
                ).fetchall()
            if not rows:
                return "Задач с ошибками нет."
            lines = []
            for r in rows:
                short_id = r["id"][:8]
                reason = r["last_error_reason"] or "—"
                icon = "🔴" if r["status"] == "requires_manual" else "⚠️"
                lines.append(f"{icon} #{short_id} [{r['status']}] {r['assigned_worker']}: {reason}")
            lines.append("\nПовторить: /retry <id>   Отменить: /cancel <id>")
            return "\n".join(lines)
        except Exception as exc:
            logger.error("handle_errors error: %s", exc)
            return "Ошибка получения списка."

    async def _handle_retry(self, task_id_prefix: str) -> str:
        """Перевести задачу error/blocked/requires_manual → pending для повторного запуска."""
        full_id = self._resolve_task_id(task_id_prefix)
        if not full_id:
            return f"Задача {task_id_prefix!r} не найдена."
        try:
            with get_conn(self._db_path) as conn:
                result = conn.execute(
                    "UPDATE tasks SET status='pending', last_error_reason=NULL, "
                    "updated_at=datetime('now') "
                    "WHERE id=? AND status IN ('error', 'blocked', 'requires_manual')",
                    (full_id,),
                )
                changed = result.rowcount > 0
            if changed:
                logger.info("task_retry task_id=%s", full_id)
                return f"↻ Задача #{task_id_prefix[:8]} поставлена в очередь повторно."
            else:
                return f"Задача #{task_id_prefix[:8]} не в статусе error/blocked/requires_manual."
        except Exception as exc:
            logger.error("handle_retry error task_id=%s: %s", full_id, exc)
            return "Ошибка при повторном запуске."

    async def _handle_cancel(self, task_id_prefix: str) -> str:
        """Отменить задачу (любой статус кроме done)."""
        full_id = self._resolve_task_id(task_id_prefix)
        if not full_id:
            return f"Задача {task_id_prefix!r} не найдена."
        try:
            with get_conn(self._db_path) as conn:
                result = conn.execute(
                    "UPDATE tasks SET status='cancelled', updated_at=datetime('now') "
                    "WHERE id=? AND status != 'done'",
                    (full_id,),
                )
                changed = result.rowcount > 0
            if changed:
                logger.info("task_cancelled task_id=%s", full_id)
                return f"✗ Задача #{task_id_prefix[:8]} отменена."
            else:
                return f"Задача #{task_id_prefix[:8]} уже завершена или не найдена."
        except Exception as exc:
            logger.error("handle_cancel error task_id=%s: %s", full_id, exc)
            return "Ошибка при отмене."

    async def _handle_plan(self, task_id_prefix: str) -> str:
        """Показать текущий план задачи."""
        full_id = self._resolve_task_id(task_id_prefix)
        if not full_id:
            return f"Задача {task_id_prefix!r} не найдена."
        try:
            with get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT plan_text, complexity, plan_revision FROM tasks WHERE id=?",
                    (full_id,),
                ).fetchone()
            if not row or not row["plan_text"]:
                return f"Задача #{task_id_prefix[:8]}: план отсутствует."

            import json  # local: avoid circular import with supervisor.planner

            try:
                plan = json.loads(row["plan_text"])
                from supervisor.planner import format_plan_for_tg  # lazy: circular dep

                return format_plan_for_tg(plan, full_id)
            except (json.JSONDecodeError, ImportError):
                # Fallback: показать raw plan_text
                text = str(row["plan_text"])[:3900]
                return f"Plan (rev {row['plan_revision']}):\n{text}"
        except Exception as exc:
            logger.error("handle_plan error task_id=%s: %s", full_id, exc)
            return "Ошибка при получении плана."

    async def _handle_revise(self, task_id_prefix: str, feedback: str) -> str:
        """Отправить план на доработку с фидбеком."""
        full_id = self._resolve_task_id(task_id_prefix)
        if not full_id:
            return f"Задача {task_id_prefix!r} не найдена."
        try:
            with get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (full_id,)
                ).fetchone()
                if not row:
                    return f"Задача #{task_id_prefix[:8]} не найдена."

                if row["status"] != "plan_review":
                    return f"Задача #{task_id_prefix[:8]} не в статусе plan_review."

                # Сигнализируем planning_stage через partial_result
                signal = f"plan:revised:{feedback}"
                conn.execute(
                    "UPDATE tasks SET partial_result=?, "
                    "updated_at=datetime('now') WHERE id=?",
                    (signal, full_id),
                )
            logger.info(
                "plan_revision_requested task_id=%s feedback=%s",
                full_id, redact(feedback[:80]),
            )
            return f"↻ Задача #{task_id_prefix[:8]}: план отправлен на доработку."
        except Exception as exc:
            logger.error("handle_revise error task_id=%s: %s", full_id, exc)
            return "Ошибка при запросе ревизии."

    async def _create_task_from_message(
        self,
        chat_id: int,
        text: str,
        username: str,
        update_id: int,
    ) -> Optional[dict]:
        """Создать задачу в DB по свободному тексту."""
        if not self._router:
            logger.warning("telegram: router not set, cannot create task for %s", username)
            return None

        try:
            worker_id = self._router.resolve_worker("telegram", username)
        except ValueError:
            logger.warning("telegram: unknown contact %s", username)
            await self.send_message(
                chat_id,
                f"Неизвестный контакт {username}. Добавьте в config/agents.yaml.",
            )
            return None

        task_id = create_task(
            source="telegram",
            source_contact=username,
            assigned_worker=worker_id,
            description=text,
            client_contact=str(chat_id),
        )
        logger.info(
            "telegram: task_created task_id=%s worker=%s update_id=%d",
            task_id, worker_id, update_id,
        )
        await self.send_message(chat_id, f"✓ Задача принята: #{task_id[:8]}")
        return {"type": "task", "task_id": task_id, "worker_id": worker_id, "update_id": update_id}

    # ── Вспомогательные методы ───────────────────────────────────────────────

    def _resolve_task_id(self, prefix: str) -> Optional[str]:
        """Найти полный task_id по короткому префиксу (первые 8 символов UUID)."""
        try:
            with get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE id LIKE ? LIMIT 1",
                    (prefix + "%",),
                ).fetchone()
            return row["id"] if row else None
        except Exception as exc:
            logger.error("resolve_task_id error prefix=%s: %s", prefix, exc)
            return None

    def _get_offset(self) -> int:
        """Загрузить last_update_id из kv_store."""
        try:
            with get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM kv_store WHERE key='tg_last_update_id'",
                ).fetchone()
            return int(row["value"]) if row else 0
        except Exception as exc:
            logger.warning("telegram: get_offset failed: %s", exc)
            return 0

    def _save_offset(self, update_id: int) -> None:
        """Сохранить update_id как новый offset в kv_store."""
        try:
            with get_conn(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO kv_store (key, value) VALUES ('tg_last_update_id', ?)",
                    (str(update_id),),
                )
        except Exception as exc:
            logger.error("telegram: save_offset failed update_id=%d: %s", update_id, exc)

    def _is_processed(self, update_id: int) -> bool:
        """Проверить, был ли update_id уже обработан (второй слой dedup)."""
        try:
            with get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM processed_tg_updates WHERE update_id=?",
                    (update_id,),
                ).fetchone()
            return row is not None
        except Exception as exc:
            logger.warning("telegram: is_processed check failed update_id=%d: %s", update_id, exc)
            return False

    def _mark_processed(self, update_id: int, task_id: Optional[str] = None) -> None:
        """Записать update_id в processed_tg_updates."""
        try:
            with get_conn(self._db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO processed_tg_updates (update_id, task_id) VALUES (?, ?)",
                    (update_id, task_id),
                )
        except Exception as exc:
            logger.error(
                "telegram: mark_processed failed update_id=%d: %s", update_id, exc,
            )

    def _write_fallback_log(self, text: str) -> None:
        """Записать недоставленное сообщение в fallback лог файл."""
        log_dir = Path(self._logs_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"tg_fallback_{date.today()}.log"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                ts = datetime.now(timezone.utc).isoformat()
                f.write(f"[{ts}] {redact(text)}\n")
        except OSError as exc:
            logger.error("telegram: fallback log write failed: %s", exc)
