"""
supervisor/run_logger.py — запись stdout/stderr агентов в файлы + task_runs.

Инварианты:
- Применяет redact() ДО записи в файл
- Пишет только в файлы и таблицу task_runs
- НЕ валидирует JSON (это обязанность json_guard)
- raw_stdout/raw_stderr ТОЛЬКО в файлах, не в DB

Формат имени файла лога:
    logs/worker_{worker_id}_{task_id[:8]}_{phase}_{ts}.log
    logs/worker_{worker_id}_{task_id[:8]}_{phase}_{ts}.err
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from storage.db import get_conn
from supervisor.log_utils import redact

logger = logging.getLogger(__name__)

DEFAULT_LOGS_DIR = "logs"


def log_run(
    task_id: str,
    phase: str,
    stdout: str,
    stderr: str,
    parsed_json: Optional[dict],
    json_valid: bool,
    started_at: Optional[datetime] = None,
    returncode: Optional[int] = None,
    attempt: int = 1,
    db_path: Optional[str] = None,
    logs_dir: Optional[str] = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    worker_id: str = "worker",
    metrics: Optional[dict] = None,
) -> tuple[str, str]:
    """
    Записать stdout/stderr агента в файлы и создать запись в task_runs.

    Args:
        task_id:     ID задачи
        phase:       фаза ("worker" | "reviewer" | "health" | "supervisor_reasoning")
        stdout:      raw stdout агента (будет redact() перед записью)
        stderr:      raw stderr агента (будет redact() перед записью)
        parsed_json: распарсенный JSON (или None)
        json_valid:  True если JSON прошёл валидацию
        started_at:  время начала (None → текущее время)
        returncode:  код возврата процесса
        attempt:     номер попытки внутри фазы
        db_path:     путь к БД
        logs_dir:    директория для лог-файлов (None → "logs/")
        now_fn:      функция текущего времени (для тестов)
        worker_id:   ID воркера (для имени файла)
        metrics:     cost/token metrics from cost_tracker (optional)

    Returns:
        Кортеж (stdout_path, stderr_path) — пути к файлам логов.
    """
    now = now_fn()
    if started_at is None:
        started_at = now

    logs_path = Path(logs_dir or DEFAULT_LOGS_DIR)
    logs_path.mkdir(parents=True, exist_ok=True)

    # Формируем имена файлов
    ts = now.strftime("%Y%m%d_%H%M%S")
    task_short = task_id[:8]
    base = f"{worker_id}_{task_short}_{phase}_{ts}"
    stdout_path = str(logs_path / f"{base}.log")
    stderr_path = str(logs_path / f"{base}.err")

    # Применяем redact() перед записью
    safe_stdout = redact(stdout) if stdout else ""
    safe_stderr = redact(stderr) if stderr else ""

    # Пишем файлы
    try:
        Path(stdout_path).write_text(safe_stdout, encoding="utf-8")
    except OSError as exc:
        logger.error(
            "run_logger: failed to write stdout log task_id=%s phase=%s: %s",
            task_id,
            phase,
            exc,
        )

    try:
        Path(stderr_path).write_text(safe_stderr, encoding="utf-8")
    except OSError as exc:
        logger.error(
            "run_logger: failed to write stderr log task_id=%s phase=%s: %s",
            task_id,
            phase,
            exc,
        )

    # Создаём запись в task_runs
    finished_at_iso = now.strftime("%Y-%m-%d %H:%M:%S")
    started_at_iso = (
        started_at.strftime("%Y-%m-%d %H:%M:%S") if started_at else finished_at_iso
    )
    parsed_json_str = json.dumps(parsed_json) if parsed_json is not None else None
    json_valid_int = 1 if json_valid else 0

    # Extract metrics if provided
    m = metrics or {}
    elapsed_ms = m.get("elapsed_ms")
    api_elapsed_ms = m.get("api_elapsed_ms")
    input_tokens = m.get("input_tokens")
    output_tokens = m.get("output_tokens")
    cache_creation_tokens = m.get("cache_creation_tokens")
    cache_read_tokens = m.get("cache_read_tokens")
    cost_usd = m.get("cost_usd")
    model_id = m.get("model_id")

    try:
        with get_conn(db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_runs
                    (task_id, phase, attempt, started_at, finished_at,
                     returncode, stdout_path, stderr_path, parsed_json, json_valid,
                     elapsed_ms, api_elapsed_ms, input_tokens, output_tokens,
                     cache_creation_tokens, cache_read_tokens, cost_usd, model_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    phase,
                    attempt,
                    started_at_iso,
                    finished_at_iso,
                    returncode,
                    stdout_path,
                    stderr_path,
                    parsed_json_str,
                    json_valid_int,
                    elapsed_ms,
                    api_elapsed_ms,
                    input_tokens,
                    output_tokens,
                    cache_creation_tokens,
                    cache_read_tokens,
                    cost_usd,
                    model_id,
                ),
            )
        logger.debug(
            "run_logger: recorded task_run task_id=%s phase=%s attempt=%d",
            task_id,
            phase,
            attempt,
        )
    except Exception as exc:
        logger.error(
            "run_logger: failed to insert task_run task_id=%s phase=%s: %s",
            task_id,
            phase,
            exc,
        )

    return stdout_path, stderr_path


def get_run_logs(
    task_id: str, phase: Optional[str] = None, db_path: Optional[str] = None
) -> list[dict]:
    """
    Получить записи task_runs для задачи.

    Args:
        task_id: ID задачи
        phase:   фильтр по фазе (None → все)
        db_path: путь к БД

    Returns:
        Список записей из task_runs в хронологическом порядке.
    """
    with get_conn(db_path) as conn:
        if phase:
            rows = conn.execute(
                """SELECT * FROM task_runs
                   WHERE task_id=? AND phase=?
                   ORDER BY started_at, id""",
                (task_id, phase),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM task_runs
                   WHERE task_id=?
                   ORDER BY started_at, id""",
                (task_id,),
            ).fetchall()
    return [dict(r) for r in rows]
