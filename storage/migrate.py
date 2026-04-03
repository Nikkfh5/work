"""
storage/migrate.py — schema versioning и идемпотентные миграции.

Все изменения схемы БД ТОЛЬКО через этот модуль.
Никогда не изменяет schema.sql напрямую.

Использование:
    from storage.migrate import apply_migrations
    from storage.db import get_conn

    with get_conn(db_path) as conn:
        apply_migrations(conn)

Инвариант: повторный вызов apply_migrations не ломает ничего (идемпотентно).
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Список миграций в порядке возрастания версий.
# Каждая миграция — (version: int, description: str, sql: str).
# Добавлять ТОЛЬКО в конец. Никогда не изменять существующие.
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "Create schema_migrations table",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            applied_at  DATETIME NOT NULL DEFAULT (datetime('now'))
        )
        """,
    ),
    (
        2,
        "Create kv_store table",
        """
        CREATE TABLE IF NOT EXISTS kv_store (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """,
    ),
    (
        3,
        "Add locked_by to tasks",
        "ALTER TABLE tasks ADD COLUMN locked_by TEXT",
    ),
    (
        4,
        "Add locked_until to tasks",
        "ALTER TABLE tasks ADD COLUMN locked_until DATETIME",
    ),
    (
        5,
        "Add lease_token to tasks",
        "ALTER TABLE tasks ADD COLUMN lease_token TEXT",
    ),
    (
        6,
        "Add worker_attempt to tasks",
        "ALTER TABLE tasks ADD COLUMN worker_attempt INTEGER NOT NULL DEFAULT 0",
    ),
    (
        7,
        "Add review_iteration to tasks",
        "ALTER TABLE tasks ADD COLUMN review_iteration INTEGER NOT NULL DEFAULT 0",
    ),
    (
        8,
        "Add last_error_code to tasks",
        "ALTER TABLE tasks ADD COLUMN last_error_code INTEGER",
    ),
    (
        9,
        "Add last_error_reason to tasks",
        "ALTER TABLE tasks ADD COLUMN last_error_reason TEXT",
    ),
    (
        10,
        "Add notion_page_url to tasks",
        "ALTER TABLE tasks ADD COLUMN notion_page_url TEXT",
    ),
    (
        11,
        "Add partial_result to tasks",
        "ALTER TABLE tasks ADD COLUMN partial_result TEXT",
    ),
    (
        12,
        "Create task_runs table",
        """
        CREATE TABLE IF NOT EXISTS task_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL REFERENCES tasks(id),
            phase       TEXT NOT NULL,
            attempt     INTEGER NOT NULL DEFAULT 1,
            started_at  DATETIME NOT NULL DEFAULT (datetime('now')),
            finished_at DATETIME,
            returncode  INTEGER,
            stdout_path TEXT,
            stderr_path TEXT,
            parsed_json TEXT,
            json_valid  INTEGER
        )
        """,
    ),
    (
        13,
        "Create processed_tg_updates table",
        """
        CREATE TABLE IF NOT EXISTS processed_tg_updates (
            update_id    INTEGER PRIMARY KEY,
            task_id      TEXT,
            processed_at DATETIME NOT NULL DEFAULT (datetime('now'))
        )
        """,
    ),
    (
        14,
        "Create processed_emails table",
        """
        CREATE TABLE IF NOT EXISTS processed_emails (
            message_id   TEXT PRIMARY KEY,
            task_id      TEXT,
            processed_at DATETIME NOT NULL DEFAULT (datetime('now'))
        )
        """,
    ),
    (
        15,
        "Add indexes for new fields",
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_status_v2
            ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_locked_until
            ON tasks(locked_until);
        CREATE INDEX IF NOT EXISTS idx_task_runs_task_phase
            ON task_runs(task_id, phase, started_at)
        """,
    ),
    (
        16,
        "Add complexity column to tasks",
        "ALTER TABLE tasks ADD COLUMN complexity TEXT",
    ),
    (
        17,
        "Add plan_text column to tasks",
        "ALTER TABLE tasks ADD COLUMN plan_text TEXT",
    ),
    (
        18,
        "Add plan_revision column to tasks",
        "ALTER TABLE tasks ADD COLUMN plan_revision INTEGER NOT NULL DEFAULT 0",
    ),
    (
        19,
        "Add cost metrics columns to task_runs",
        """
        ALTER TABLE task_runs ADD COLUMN elapsed_ms INTEGER;
        ALTER TABLE task_runs ADD COLUMN api_elapsed_ms INTEGER;
        ALTER TABLE task_runs ADD COLUMN input_tokens INTEGER;
        ALTER TABLE task_runs ADD COLUMN output_tokens INTEGER;
        ALTER TABLE task_runs ADD COLUMN cache_creation_tokens INTEGER;
        ALTER TABLE task_runs ADD COLUMN cache_read_tokens INTEGER;
        ALTER TABLE task_runs ADD COLUMN cost_usd REAL;
        ALTER TABLE task_runs ADD COLUMN model_id TEXT
        """,
    ),
    (
        20,
        "Create session_checkpoints table",
        """
        CREATE TABLE IF NOT EXISTS session_checkpoints (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         TEXT NOT NULL REFERENCES tasks(id),
            worker_id       TEXT NOT NULL,
            phase           TEXT NOT NULL,
            attempt         INTEGER,
            checkpoint_at   DATETIME NOT NULL DEFAULT (datetime('now')),
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            cost_usd        REAL,
            progress_summary TEXT,
            saved_context    TEXT,
            resumed          INTEGER NOT NULL DEFAULT 0
        )
        """,
    ),
]


def _get_version(conn: sqlite3.Connection) -> int:
    """Получить текущую версию схемы. Возвращает 0 если таблицы нет."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        return row[0] or 0
    except sqlite3.OperationalError:
        # Таблица schema_migrations ещё не существует
        return 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    """Записать версию в schema_migrations."""
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations (version) VALUES (?)",
        (version,),
    )


def apply_migrations(conn: sqlite3.Connection) -> int:
    """
    Применить все недостающие миграции к открытому соединению.

    Args:
        conn: открытое sqlite3 соединение (без autocommit).
              Вызывающий отвечает за commit/rollback.

    Returns:
        Количество применённых миграций (0 если всё уже актуально).

    Raises:
        sqlite3.Error: при ошибке применения миграции
    """
    current = _get_version(conn)
    applied = 0

    for version, description, sql in MIGRATIONS:
        if version <= current:
            continue

        logger.info("Applying migration %d: %s", version, description)
        try:
            # Некоторые миграции содержат несколько операторов
            conn.executescript(sql)
            # executescript делает commit — нужно вернуть FK
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.OperationalError as exc:
            # ALTER TABLE ADD COLUMN — если колонка уже есть (idempotent)
            if "duplicate column name" in str(exc).lower():
                logger.warning(
                    "Migration %d skipped (column already exists): %s",
                    version, exc,
                )
            else:
                logger.error("Migration %d failed: %s", version, exc)
                raise

        _set_version(conn, version)
        conn.commit()
        applied += 1
        logger.info("Migration %d applied successfully", version)

    if applied == 0:
        logger.debug("Schema is up to date (version %d)", current)
    else:
        logger.info(
            "Applied %d migration(s), schema now at version %d",
            applied,
            _get_version(conn),
        )

    return applied
