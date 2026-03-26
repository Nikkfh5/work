"""
tests/test_migrate.py — тесты для storage/migrate.py

Запуск: pytest tests/test_migrate.py -v
"""

import pytest
from storage.db import init_db, get_conn
from storage.migrate import apply_migrations, MIGRATIONS, _get_version


@pytest.fixture
def fresh_db(tmp_path):
    """Свежая БД со схемой из schema.sql (без миграций)."""
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def test_apply_migrations_creates_schema_migrations_table(fresh_db):
    """После apply_migrations таблица schema_migrations должна существовать."""
    with get_conn(fresh_db) as conn:
        apply_migrations(conn)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "schema_migrations" in tables


def test_apply_migrations_creates_all_new_tables(fresh_db):
    """Все новые таблицы из плана должны быть созданы."""
    with get_conn(fresh_db) as conn:
        apply_migrations(conn)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "task_runs" in tables
    assert "processed_tg_updates" in tables
    assert "processed_emails" in tables
    assert "kv_store" in tables


def test_apply_migrations_adds_columns_to_tasks(fresh_db):
    """Новые колонки должны быть добавлены в таблицу tasks."""
    with get_conn(fresh_db) as conn:
        apply_migrations(conn)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
    expected_columns = {
        "locked_by",
        "locked_until",
        "lease_token",
        "worker_attempt",
        "review_iteration",
        "last_error_code",
        "last_error_reason",
        "notion_page_url",
        "partial_result",
    }
    assert expected_columns.issubset(columns)


def test_apply_migrations_idempotent(fresh_db):
    """Повторный вызов apply_migrations не ломает ничего."""
    with get_conn(fresh_db) as conn:
        first = apply_migrations(conn)
    with get_conn(fresh_db) as conn:
        second = apply_migrations(conn)
    assert first > 0
    assert second == 0  # уже всё применено


def test_version_increments_correctly(fresh_db):
    """Версия должна соответствовать максимальному номеру миграции."""
    max_version = max(v for v, _, _ in MIGRATIONS)
    with get_conn(fresh_db) as conn:
        apply_migrations(conn)
        version = _get_version(conn)
    assert version == max_version


def test_existing_data_preserved_after_migration(fresh_db):
    """Данные в tasks не теряются при миграции."""
    from storage.db import create_task
    import os

    os.environ["DB_PATH"] = fresh_db
    task_id = create_task(
        source="telegram",
        source_contact="@test",
        assigned_worker="python_1",
        description="Test task",
        client_contact="@test",
    )
    with get_conn(fresh_db) as conn:
        apply_migrations(conn)
    with get_conn(fresh_db) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row is not None
    assert row["description"] == "Test task"
    del os.environ["DB_PATH"]


def test_task_runs_table_structure(fresh_db):
    """Таблица task_runs имеет правильную структуру."""
    with get_conn(fresh_db) as conn:
        apply_migrations(conn)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()
        }
    expected = {
        "id",
        "task_id",
        "phase",
        "attempt",
        "started_at",
        "finished_at",
        "returncode",
        "stdout_path",
        "stderr_path",
        "parsed_json",
        "json_valid",
    }
    assert expected.issubset(columns)


def test_kv_store_usable_after_migration(fresh_db):
    """kv_store можно читать/писать после миграции."""
    with get_conn(fresh_db) as conn:
        apply_migrations(conn)
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?,?)",
            ("test_key", "test_value"),
        )
    with get_conn(fresh_db) as conn:
        row = conn.execute(
            "SELECT value FROM kv_store WHERE key=?", ("test_key",)
        ).fetchone()
    assert row[0] == "test_value"
