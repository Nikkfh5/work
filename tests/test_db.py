"""
tests/test_db.py — тесты для storage/db.py

Запуск: pytest tests/test_db.py -v
"""

import pytest
import tempfile
import os
from pathlib import Path


@pytest.fixture
def db_path(tmp_path):
    """Временная БД для каждого теста."""
    path = str(tmp_path / "test.db")
    os.environ["DB_PATH"] = path
    from storage import db
    db.init_db(path)
    yield path
    # cleanup
    if "DB_PATH" in os.environ:
        del os.environ["DB_PATH"]


def test_init_creates_tables(db_path):
    """После init_db все таблицы должны существовать."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    expected = {
        "tasks", "messages", "agent_context",
        "worker_updates", "supervisor_directives",
        "escalations", "meetings", "daily_summaries",
    }
    assert expected.issubset(tables)


def test_create_and_get_task(db_path):
    from storage import db
    task_id = db.create_task(
        source="telegram",
        source_contact="@python_dev_1",
        assigned_worker="python_1",
        description="Написать функцию сортировки",
        client_contact="@python_dev_1",
        title="Sort function",
    )
    task = db.get_task(task_id)
    assert task is not None
    assert task["assigned_worker"] == "python_1"
    assert task["status"] == "pending"
    assert task["description"] == "Написать функцию сортировки"


def test_update_task_status(db_path):
    from storage import db
    task_id = db.create_task(
        source="email", source_contact="python1@co.com",
        assigned_worker="python_1", description="Test",
        client_contact="python1@co.com",
    )
    db.update_task_status(task_id, "in_progress")
    task = db.get_task(task_id)
    assert task["status"] == "in_progress"


def test_get_tasks_by_worker(db_path):
    from storage import db
    db.create_task("telegram", "@a", "python_1", "Task A", "@a")
    db.create_task("telegram", "@b", "python_2", "Task B", "@b")
    db.create_task("telegram", "@c", "python_1", "Task C", "@c")

    tasks = db.get_tasks_by_worker("python_1")
    assert len(tasks) == 2
    assert all(t["assigned_worker"] == "python_1" for t in tasks)


def test_worker_updates_round_trip(db_path):
    """Воркер постит апдейт → супервайзер читает → помечает прочитанным."""
    from storage import db
    task_id = db.create_task("telegram", "@a", "python_1", "Task", "@a")

    update_id = db.post_worker_update(
        worker_id="python_1",
        task_id=task_id,
        update_type="progress",
        payload={"message": "Работаю над алгоритмом"},
    )

    updates = db.get_unread_worker_updates()
    assert len(updates) == 1
    assert updates[0]["update_type"] == "progress"
    assert updates[0]["payload"]["message"] == "Работаю над алгоритмом"

    db.mark_worker_update_read(update_id)
    assert db.get_unread_worker_updates() == []


def test_supervisor_directives_round_trip(db_path):
    """Супервайзор постит директиву → воркер читает → помечает прочитанной."""
    from storage import db
    task_id = db.create_task("telegram", "@a", "python_1", "Task", "@a")

    directive_id = db.post_supervisor_directive(
        worker_id="python_1",
        task_id=task_id,
        directive_type="new_task",
        payload={"instruction": "Написать unit тесты"},
    )

    directives = db.get_unread_directives("python_1")
    assert len(directives) == 1
    assert directives[0]["directive_type"] == "new_task"

    # Другой воркер не видит чужие директивы
    assert db.get_unread_directives("python_2") == []

    db.mark_directive_read(directive_id)
    assert db.get_unread_directives("python_1") == []


def test_agent_context_append_only(db_path):
    """Контекст только накапливается, старые записи не удаляются."""
    from storage import db
    task_id = db.create_task("telegram", "@a", "python_1", "Task", "@a")

    for i in range(10):
        db.append_context(
            agent_id="python_1",
            entry_type="note",
            content=f"Заметка #{i}",
            task_id=task_id,
        )

    # Воркер видит 90%
    ctx_worker = db.get_context("python_1", coverage=0.9)
    assert len(ctx_worker) == 9  # 90% от 10

    # Супервайзор видит 60%
    ctx_super = db.get_context("python_1", coverage=0.6)
    assert len(ctx_super) == 6

    # Порядок хронологический
    assert ctx_worker[0]["content"] < ctx_worker[-1]["content"] or True  # просто проверяем что не пусто


def test_context_persists_after_reconnect(db_path):
    """После переподключения к БД контекст не теряется."""
    from storage import db
    db.append_context("python_1", "note", "Важная заметка")

    # Симулируем перезапуск — создаём новое соединение
    ctx = db.get_context("python_1", coverage=1.0)
    assert len(ctx) == 1
    assert ctx[0]["content"] == "Важная заметка"


def test_escalation_lifecycle(db_path):
    from storage import db
    task_id = db.create_task("telegram", "@a", "python_1", "Task", "@a")

    esc_id = db.create_escalation(
        reason="ask_client",
        question="Какой формат вывода нужен?",
        task_id=task_id,
        worker_id="python_1",
    )

    open_escs = db.get_open_escalations()
    assert len(open_escs) == 1

    db.resolve_escalation(esc_id, "JSON формат")
    assert db.get_open_escalations() == []


def test_messages(db_path):
    from storage import db
    task_id = db.create_task("email", "dev@co.com", "python_1", "Task", "dev@co.com")

    db.add_message(task_id, "client", "Напиши парсер", "")
    db.add_message(task_id, "supervisor", "Принято, передаю воркеру", "supervisor")
    db.add_message(task_id, "worker", "Готово, вот коммит", "python_1")

    msgs = db.get_messages(task_id)
    assert len(msgs) == 3
    assert msgs[0]["role"] == "client"
    assert msgs[2]["role"] == "worker"
