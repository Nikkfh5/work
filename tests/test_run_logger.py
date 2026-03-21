"""
tests/test_run_logger.py — тесты для supervisor/run_logger.py

Запуск: pytest tests/test_run_logger.py -v
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from storage.db import create_task
from supervisor.run_logger import log_run, get_run_logs


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return str(d)


@pytest.fixture
def task_id(db_path):
    return create_task(
        source="telegram",
        source_contact="@test",
        assigned_worker="job1_worker",
        description="Test task",
        client_contact="@test",
    )


def fixed_now():
    return datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)


# ── Тесты записи файлов ──────────────────────────────────────────────────────


def test_log_run_creates_stdout_file(db_path, logs_dir, task_id):
    """log_run создаёт файл stdout."""
    stdout_path, _ = log_run(
        task_id=task_id,
        phase="worker",
        stdout="результат работы",
        stderr="",
        parsed_json=None,
        json_valid=False,
        db_path=db_path,
        logs_dir=logs_dir,
        now_fn=fixed_now,
        worker_id="job1_worker",
    )
    assert Path(stdout_path).exists()
    assert Path(stdout_path).read_text(encoding="utf-8") == "результат работы"


def test_log_run_creates_stderr_file(db_path, logs_dir, task_id):
    """log_run создаёт файл stderr."""
    _, stderr_path = log_run(
        task_id=task_id,
        phase="worker",
        stdout="",
        stderr="ошибка",
        parsed_json=None,
        json_valid=False,
        db_path=db_path,
        logs_dir=logs_dir,
        now_fn=fixed_now,
    )
    assert Path(stderr_path).exists()
    assert "ошибка" in Path(stderr_path).read_text(encoding="utf-8")


def test_log_run_redacts_secrets_in_files(db_path, logs_dir, task_id):
    """Секреты удаляются перед записью в файл."""
    stdout_path, _ = log_run(
        task_id=task_id,
        phase="worker",
        stdout="git push with token ghp_AbCdEfGhIjKlMnOpQrStUvWx1234",
        stderr="",
        parsed_json=None,
        json_valid=False,
        db_path=db_path,
        logs_dir=logs_dir,
        now_fn=fixed_now,
    )
    content = Path(stdout_path).read_text(encoding="utf-8")
    assert "ghp_" not in content
    assert "[GITHUB_TOKEN]" in content


def test_log_run_file_names_contain_phase(db_path, logs_dir, task_id):
    """Имена файлов содержат phase."""
    stdout_path, stderr_path = log_run(
        task_id=task_id,
        phase="reviewer",
        stdout="ok",
        stderr="",
        parsed_json=None,
        json_valid=False,
        db_path=db_path,
        logs_dir=logs_dir,
        now_fn=fixed_now,
    )
    assert "reviewer" in stdout_path
    assert "reviewer" in stderr_path


def test_log_run_empty_stdout_stderr(db_path, logs_dir, task_id):
    """Пустые stdout/stderr — файлы создаются но пустые."""
    stdout_path, stderr_path = log_run(
        task_id=task_id,
        phase="worker",
        stdout="",
        stderr="",
        parsed_json=None,
        json_valid=False,
        db_path=db_path,
        logs_dir=logs_dir,
        now_fn=fixed_now,
    )
    assert Path(stdout_path).read_text() == ""
    assert Path(stderr_path).read_text() == ""


# ── Тесты записи в task_runs ─────────────────────────────────────────────────


def test_log_run_creates_task_run_record(db_path, logs_dir, task_id):
    """log_run создаёт запись в task_runs."""
    log_run(
        task_id=task_id,
        phase="worker",
        stdout="output",
        stderr="",
        parsed_json={"status": "done"},
        json_valid=True,
        returncode=0,
        attempt=1,
        db_path=db_path,
        logs_dir=logs_dir,
        now_fn=fixed_now,
    )
    runs = get_run_logs(task_id=task_id, db_path=db_path)
    assert len(runs) == 1
    run = runs[0]
    assert run["task_id"] == task_id
    assert run["phase"] == "worker"
    assert run["attempt"] == 1
    assert run["returncode"] == 0
    assert run["json_valid"] == 1


def test_log_run_stores_paths_in_task_runs(db_path, logs_dir, task_id):
    """task_runs хранит пути к файлам, не содержимое."""
    stdout_path, stderr_path = log_run(
        task_id=task_id,
        phase="worker",
        stdout="output",
        stderr="err",
        parsed_json=None,
        json_valid=False,
        db_path=db_path,
        logs_dir=logs_dir,
        now_fn=fixed_now,
    )
    runs = get_run_logs(task_id=task_id, db_path=db_path)
    assert runs[0]["stdout_path"] == stdout_path
    assert runs[0]["stderr_path"] == stderr_path
    # В DB не хранится само содержимое
    assert "output" not in (runs[0]["stdout_path"] or "")


def test_log_run_stores_parsed_json(db_path, logs_dir, task_id):
    """parsed_json сериализуется в task_runs.parsed_json."""
    obj = {"status": "done", "confidence": 85}
    log_run(
        task_id=task_id,
        phase="worker",
        stdout="",
        stderr="",
        parsed_json=obj,
        json_valid=True,
        db_path=db_path,
        logs_dir=logs_dir,
        now_fn=fixed_now,
    )
    runs = get_run_logs(task_id=task_id, db_path=db_path)
    stored = json.loads(runs[0]["parsed_json"])
    assert stored["status"] == "done"
    assert stored["confidence"] == 85


def test_log_run_multiple_attempts(db_path, logs_dir, task_id):
    """Несколько вызовов log_run создают несколько записей."""
    for attempt in range(1, 4):
        log_run(
            task_id=task_id,
            phase="worker",
            stdout=f"attempt {attempt}",
            stderr="",
            parsed_json=None,
            json_valid=False,
            attempt=attempt,
            db_path=db_path,
            logs_dir=logs_dir,
            now_fn=fixed_now,
        )
    runs = get_run_logs(task_id=task_id, db_path=db_path)
    assert len(runs) == 3
    attempts = [r["attempt"] for r in runs]
    assert sorted(attempts) == [1, 2, 3]


def test_get_run_logs_filter_by_phase(db_path, logs_dir, task_id):
    """get_run_logs фильтрует по phase."""
    log_run(
        task_id=task_id,
        phase="worker",
        stdout="w",
        stderr="",
        parsed_json=None,
        json_valid=False,
        db_path=db_path,
        logs_dir=logs_dir,
    )
    log_run(
        task_id=task_id,
        phase="reviewer",
        stdout="r",
        stderr="",
        parsed_json=None,
        json_valid=False,
        db_path=db_path,
        logs_dir=logs_dir,
    )

    worker_runs = get_run_logs(task_id=task_id, phase="worker", db_path=db_path)
    reviewer_runs = get_run_logs(task_id=task_id, phase="reviewer", db_path=db_path)
    all_runs = get_run_logs(task_id=task_id, db_path=db_path)

    assert len(worker_runs) == 1
    assert len(reviewer_runs) == 1
    assert len(all_runs) == 2
