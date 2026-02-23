"""
storage/db.py — единственная точка доступа к SQLite.
Все агенты используют этот модуль для чтения/записи.
"""

import sqlite3
import uuid
import json
import logging
import os
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_db_path() -> str:
    return os.getenv("DB_PATH", "data/orchestrator.db")


def init_db(db_path: Optional[str] = None) -> None:
    """Инициализировать БД из schema.sql. Вызывать один раз при старте."""
    path = db_path or get_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
    logger.info("DB initialized at %s", path)


@contextmanager
def get_conn(db_path: Optional[str] = None):
    """Контекст-менеджер для соединения с БД."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")  # 10s ждать блокировку
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _new_id() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────
# Tasks
# ─────────────────────────────────────────────────────────

def create_task(
    source: str,
    source_contact: str,
    assigned_worker: str,
    description: str,
    client_contact: str,
    title: str = "",
    priority: str = "normal",
    git_repo: str = "",
) -> str:
    """Создать задачу. Возвращает task_id."""
    task_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, source, source_contact, assigned_worker, description,
                client_contact, title, priority, git_repo)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (task_id, source, source_contact, assigned_worker, description,
             client_contact, title, priority, git_repo),
        )
    logger.debug("Task created: %s → %s", task_id, assigned_worker)
    return task_id


def get_task(task_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def update_task_status(task_id: str, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, task_id),
        )


def get_tasks_by_worker(worker_id: str, status: Optional[str] = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE assigned_worker=? AND status=? ORDER BY created_at",
                (worker_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE assigned_worker=? ORDER BY created_at",
                (worker_id,),
            ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────
# Worker Updates (воркер → супервайзер)
# ─────────────────────────────────────────────────────────

def post_worker_update(
    worker_id: str,
    task_id: str,
    update_type: str,
    payload: dict,
) -> str:
    """Воркер отправляет обновление супервайзеру."""
    update_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO worker_updates
               (id, worker_id, task_id, update_type, payload)
               VALUES (?,?,?,?,?)""",
            (update_id, worker_id, task_id, update_type, json.dumps(payload)),
        )
    logger.debug("Worker update posted: %s %s → supervisor", update_type, worker_id)
    return update_id


def get_unread_worker_updates() -> list[dict]:
    """Супервайзер читает все необработанные обновления от воркеров."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM worker_updates
               WHERE read_by_supervisor=0
               ORDER BY created_at""",
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        result.append(d)
    return result


def mark_worker_update_read(update_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE worker_updates SET read_by_supervisor=1, processed_at=datetime('now') WHERE id=?",
            (update_id,),
        )


# ─────────────────────────────────────────────────────────
# Supervisor Directives (супервайзер → воркер)
# ─────────────────────────────────────────────────────────

def post_supervisor_directive(
    worker_id: str,
    task_id: str,
    directive_type: str,
    payload: dict,
) -> str:
    """Супервайзер отправляет директиву воркеру."""
    directive_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO supervisor_directives
               (id, worker_id, task_id, directive_type, payload)
               VALUES (?,?,?,?,?)""",
            (directive_id, worker_id, task_id, directive_type, json.dumps(payload)),
        )
    logger.debug("Directive posted: %s → %s", directive_type, worker_id)
    return directive_id


def get_unread_directives(worker_id: str) -> list[dict]:
    """Воркер читает свои необработанные директивы."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM supervisor_directives
               WHERE worker_id=? AND read_by_worker=0
               ORDER BY created_at""",
            (worker_id,),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        result.append(d)
    return result


def mark_directive_read(directive_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE supervisor_directives SET read_by_worker=1, read_at=datetime('now') WHERE id=?",
            (directive_id,),
        )


# ─────────────────────────────────────────────────────────
# Agent Context (append-only)
# ─────────────────────────────────────────────────────────

def append_context(
    agent_id: str,
    entry_type: str,
    content: str,
    task_id: Optional[str] = None,
    importance: str = "normal",
) -> str:
    """Добавить запись в контекст агента. Никогда не удаляет старые."""
    entry_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agent_context
               (id, agent_id, entry_type, task_id, content, importance)
               VALUES (?,?,?,?,?,?)""",
            (entry_id, agent_id, entry_type, task_id, content, importance),
        )
    return entry_id


def get_context(agent_id: str, coverage: float = 0.9) -> list[dict]:
    """
    Загрузить контекст агента.
    coverage=0.9 → последние 90% записей (для воркера)
    coverage=0.6 → последние 60% записей (для супервайзера о воркере)
    """
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM agent_context WHERE agent_id=?", (agent_id,)
        ).fetchone()[0]
        limit = max(1, int(total * coverage))
        rows = conn.execute(
            """SELECT * FROM agent_context WHERE agent_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (agent_id, limit),
        ).fetchall()
    # Вернуть в хронологическом порядке
    return [dict(r) for r in reversed(rows)]


# ─────────────────────────────────────────────────────────
# Messages
# ─────────────────────────────────────────────────────────

def add_message(task_id: str, role: str, content: str, agent_id: str = "") -> str:
    msg_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (id, task_id, role, agent_id, content) VALUES (?,?,?,?,?)",
            (msg_id, task_id, role, agent_id, content),
        )
    return msg_id


def get_messages(task_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE task_id=? ORDER BY created_at",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────
# Escalations
# ─────────────────────────────────────────────────────────

def create_escalation(
    reason: str,
    question: str,
    task_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    context: Optional[str] = None,
) -> str:
    esc_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO escalations
               (id, task_id, worker_id, reason, question, context)
               VALUES (?,?,?,?,?,?)""",
            (esc_id, task_id, worker_id, reason, question, context),
        )
    return esc_id


def resolve_escalation(esc_id: str, response: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE escalations
               SET resolved=1, response=?, resolved_at=datetime('now')
               WHERE id=?""",
            (response, esc_id),
        )


def get_open_escalations() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM escalations WHERE resolved=0 ORDER BY created_at",
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────
# Meetings
# ─────────────────────────────────────────────────────────

def save_meeting(
    title: str,
    meeting_date: str,
    summary: str,
    participants: list[str],
    platform: str = "",
    transcript: str = "",
    external_id: str = "",
) -> str:
    meeting_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO meetings
               (id, title, platform, external_id, transcript, summary, participants, meeting_date)
               VALUES (?,?,?,?,?,?,?,?)""",
            (meeting_id, title, platform, external_id, transcript, summary,
             json.dumps(participants), meeting_date),
        )
    return meeting_id


# ─────────────────────────────────────────────────────────
# Daily Summaries
# ─────────────────────────────────────────────────────────

def save_daily_summary(content: str) -> str:
    summary_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_summaries (id, content) VALUES (?,?)",
            (summary_id, content),
        )
    return summary_id


def mark_summary_sent(summary_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE daily_summaries SET sent=1 WHERE id=?", (summary_id,)
        )
