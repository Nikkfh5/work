"""
tests/conftest.py — общие фикстуры для всех тест-файлов.

pytest автоматически подхватывает conftest.py — импорт не нужен.
"""

import os

import pytest
from unittest.mock import AsyncMock

from storage.db import init_db, get_conn
from storage.migrate import apply_migrations


# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    """Временная БД с полной схемой и миграциями. Устанавливает DB_PATH env."""
    path = str(tmp_path / "test.db")
    os.environ["DB_PATH"] = path
    init_db(path)
    with get_conn(path) as conn:
        apply_migrations(conn)
    yield path
    if "DB_PATH" in os.environ:
        del os.environ["DB_PATH"]


@pytest.fixture
def mock_tg_handler():
    """Мок TelegramHandler с notify_owner и poll_once."""
    handler = AsyncMock()
    handler.notify_owner = AsyncMock(return_value=True)
    handler.poll_once = AsyncMock(return_value=[])
    return handler


# ── Shared constants ─────────────────────────────────────────────────────────

WORKER_DONE_JSON = """\
<<<JSON>>>
{
  "status": "done",
  "confidence": 90,
  "result": {"repos": [{"alias": "api", "changed_files": ["app.py"], "entrypoint": null}], "notes": "готово"},
  "question": null
}
<<<END>>>
"""
