"""
supervisor/health_monitor.py — периодическая проверка здоровья системы.

Собирает snapshot (статистика задач, диск, логи),
отправляет health-агенту для классификации (GREEN/YELLOW/RED),
выполняет auto_actions из allowlist, создаёт задачи из create_tasks.

Инварианты:
- build_snapshot собирает данные из DB и файловой системы
- run_health_check вызывает claude CLI через runner (DI)
- auto_actions выполняются только из allowlist (Python functions, не subprocess)
- supervisor — единственный кто пишет клиенту (TG alert)
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from storage.db import get_conn, create_task
from supervisor.json_guard import extract_json, validate_health_schema
from supervisor.lease_manager import release_stale
from supervisor.repo_manager import RepoManager

logger = logging.getLogger(__name__)

# Retention for rotate_logs auto_action (days)
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))

# Allowed auto_actions from health agent
ALLOWED_AUTO_ACTIONS = frozenset(
    {
        "cleanup_worktrees",
        "release_stale_leases",
        "rotate_logs",
        "gc_mirrors",
    }
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_dir_size(path: str) -> int:
    """
    Рекурсивный размер директории в байтах.

    Если директория не существует — возвращает 0.
    Пропускает ошибки доступа (Permission, OSError).
    """
    total = 0
    target = Path(path)
    if not target.exists():
        return 0
    try:
        for entry in target.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except (OSError, PermissionError):
                pass
    except (OSError, PermissionError):
        pass
    return total


def _format_bytes(size: int) -> str:
    """
    Форматирование размера: 1024 -> '1.0 KB', 1048576 -> '1.0 MB'.

    Поддерживает B, KB, MB, GB, TB.
    """
    if size < 0:
        size = 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


# ── Snapshot ─────────────────────────────────────────────────────────────────


async def build_snapshot(
    db_path: Optional[str] = None,
    config: Optional[dict] = None,
) -> str:
    """
    Собрать текстовый snapshot системы для health-агента.

    Содержимое:
    - Текущее время
    - Tasks агрегаты по статусам
    - Ошибки за 24ч
    - Tasks в requires_manual
    - Stale leases
    - Диск: worktrees/, repos_cache/, logs/
    - Последние 50 строк logs/supervisor.log

    Returns:
        Форматированный текст для промпта health-агента.
    """
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    lines.append(
        f"=== System Health Snapshot ({now.strftime('%Y-%m-%d %H:%M:%S UTC')}) ==="
    )
    lines.append("")

    # ── Task statistics ──
    lines.append("## Task Statistics")
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
        ).fetchall()
    if rows:
        for row in rows:
            lines.append(f"  {row['status']}: {row['cnt']}")
    else:
        lines.append("  (no tasks)")
    lines.append("")

    # ── Errors in last 24h ──
    lines.append("## Errors (last 24h)")
    with get_conn(db_path) as conn:
        error_rows = conn.execute(
            """
            SELECT last_error_reason, COUNT(*) AS cnt
            FROM tasks
            WHERE last_error_reason IS NOT NULL
              AND updated_at >= datetime('now', '-24 hours')
            GROUP BY last_error_reason
            ORDER BY cnt DESC
            """
        ).fetchall()
    if error_rows:
        for row in error_rows:
            lines.append(f"  {row['last_error_reason']}: {row['cnt']}")
    else:
        lines.append("  (no errors)")
    lines.append("")

    # ── Tasks requiring manual intervention ──
    lines.append("## Tasks Requiring Manual Intervention")
    with get_conn(db_path) as conn:
        manual_rows = conn.execute(
            """
            SELECT id, description, last_error_reason
            FROM tasks
            WHERE status = 'requires_manual'
            """
        ).fetchall()
    if manual_rows:
        for row in manual_rows:
            desc_short = (row["description"] or "")[:80]
            lines.append(
                f"  - {row['id'][:8]}... : {desc_short} (error: {row['last_error_reason']})"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # ── Stale leases ──
    lines.append("## Potentially Stale Leases")
    worker_timeout = int(os.getenv("WORKER_TIMEOUT_SECONDS", "1800"))
    stale_threshold_seconds = worker_timeout // 2
    with get_conn(db_path) as conn:
        stale_rows = conn.execute(
            f"""
            SELECT id, assigned_worker, locked_by, locked_until, updated_at
            FROM tasks
            WHERE status = 'running'
              AND updated_at <= datetime('now', '-{stale_threshold_seconds} seconds')
            """
        ).fetchall()
    if stale_rows:
        for row in stale_rows:
            lines.append(
                f"  - {row['id'][:8]}... worker={row['assigned_worker']} "
                f"locked_until={row['locked_until']} updated={row['updated_at']}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # ── Disk usage ──
    lines.append("## Disk Usage")
    for dir_name in ("worktrees", "repos_cache", "logs"):
        size = _get_dir_size(dir_name)
        lines.append(f"  {dir_name}/: {_format_bytes(size)}")
    lines.append("")

    # ── Last 50 lines of supervisor.log ──
    lines.append("## Recent Supervisor Log (last 50 lines)")
    log_path = Path("logs") / "supervisor.log"
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            tail = all_lines[-50:] if len(all_lines) > 50 else all_lines
            for line in tail:
                lines.append(f"  {line.rstrip()}")
        except OSError as exc:
            lines.append(f"  (error reading log: {exc})")
    else:
        lines.append("  (file not found)")
    lines.append("")

    return "\n".join(lines)


# ── Auto-actions ─────────────────────────────────────────────────────────────


async def _execute_auto_actions(
    actions: list[str],
    db_path: Optional[str] = None,
) -> dict[str, str]:
    """
    Выполнить auto_actions из allowlist.

    Returns:
        dict: action_name -> result/error string.
    """
    results: dict[str, str] = {}

    for action in actions:
        if action not in ALLOWED_AUTO_ACTIONS:
            logger.warning("health_monitor: skipping unknown auto_action=%s", action)
            results[action] = "skipped: not in allowlist"
            continue

        try:
            if action == "cleanup_worktrees":
                retention = int(os.getenv("WORKTREE_RETENTION_DAYS", "7"))
                repo_mgr = RepoManager()
                removed = repo_mgr.cleanup_old_worktrees(retention_days=retention)
                results[action] = f"removed {removed} old worktree(s)"
                logger.info("health_monitor: cleanup_worktrees removed %d", removed)

            elif action == "release_stale_leases":
                count = release_stale(db_path=db_path)
                results[action] = f"released {count} stale lease(s)"
                logger.info("health_monitor: release_stale_leases released %d", count)

            elif action == "rotate_logs":
                removed = _rotate_logs(LOG_RETENTION_DAYS)
                results[action] = f"removed {removed} old log file(s)"
                logger.info("health_monitor: rotate_logs removed %d files", removed)

            elif action == "gc_mirrors":
                logger.info("health_monitor: gc_mirrors not implemented yet, skipping")
                results[action] = "not implemented"

        except Exception as exc:
            logger.error("health_monitor: auto_action=%s failed: %s", action, exc)
            results[action] = f"error: {exc}"

    return results


def _rotate_logs(retention_days: int) -> int:
    """Удалить лог-файлы старше retention_days дней."""
    logs_dir = Path("logs")
    if not logs_dir.exists():
        return 0

    now = datetime.now(timezone.utc)
    removed = 0

    for log_file in logs_dir.iterdir():
        if not log_file.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc)
            age_days = (now - mtime).days
            if age_days > retention_days:
                log_file.unlink()
                logger.debug(
                    "rotate_logs: removed %s (age=%d days)", log_file, age_days
                )
                removed += 1
        except OSError as exc:
            logger.warning("rotate_logs: error processing %s: %s", log_file, exc)

    return removed


# ── Create tasks from health findings ────────────────────────────────────────


def _create_health_tasks(
    create_tasks: list[dict],
    db_path: Optional[str] = None,
) -> list[str]:
    """
    Создать задачи из create_tasks health-агента со статусом pending_approval.

    Returns:
        Список task_id созданных задач.
    """
    task_ids: list[str] = []

    for task_spec in create_tasks:
        title = task_spec.get("title", "Health task")
        summary = task_spec.get("summary", "")
        priority = task_spec.get("priority", "normal")

        # Map P1/P2/P3 to system priority
        priority_map = {"P1": "urgent", "P2": "high", "P3": "normal"}
        mapped_priority = priority_map.get(priority, priority)

        task_id = create_task(
            source="health_monitor",
            source_contact="system",
            assigned_worker="health_monitor",
            description=f"{title}\n\n{summary}",
            client_contact="system",
            title=title,
            priority=mapped_priority,
        )

        # Set status to pending_approval
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = 'pending_approval' WHERE id = ?",
                (task_id,),
            )

        logger.info(
            "health_monitor: created task_id=%s title=%s priority=%s",
            task_id,
            title,
            mapped_priority,
        )
        task_ids.append(task_id)

    return task_ids


# ── Main health check ────────────────────────────────────────────────────────


def _build_health_prompt(snapshot: str) -> str:
    """Построить промпт для health-агента."""
    return f"""You are the health monitor agent. Analyze the following system snapshot and respond with a health assessment.

{snapshot}

Respond with JSON between markers:
<<<JSON>>>
{{
  "status": "done",
  "confidence": 0-100,
  "health": {{
    "overall": "GREEN|YELLOW|RED",
    "findings": [{{"severity": "low|med|high", "title": "...", "evidence": "...", "suggestion": "..."}}],
    "auto_actions": [],
    "create_tasks": [{{"title": "...", "summary": "...", "priority": "P1|P2|P3"}}],
    "tg_alert": "one-line alert for Telegram (only if YELLOW/RED)",
    "notion_detail": "detailed markdown (only if YELLOW/RED)"
  }},
  "question": null
}}
<<<END>>>

Rules:
- Only use findings from the actual data above — do not fabricate evidence
- auto_actions MUST be from: cleanup_worktrees, rotate_logs, gc_mirrors, release_stale_leases
- GREEN: everything looks fine, no action needed
- YELLOW: minor issues, auto_actions may help
- RED: critical issues, needs manual intervention
"""


async def run_health_check(
    tg_handler,
    db_path: Optional[str] = None,
    config: Optional[dict] = None,
    runner: Optional[Callable] = None,
) -> Optional[dict]:
    """
    Полный цикл health check.

    1. build_snapshot()
    2. Построить промпт для health агента
    3. run_claude (или runner если передан — DI pattern) в cwd=workers/health_monitor/
    4. extract_json + validate_health_schema
    5. Execute auto_actions из allowlist
    6. Для create_tasks -> создать задачи в DB со статусом pending_approval
    7. Если RED/YELLOW -> TG alert. Если GREEN -> только лог.
    8. Return parsed health result

    Args:
        tg_handler: TelegramHandler для отправки алертов
        db_path: путь к БД
        config: конфиг из agents.yaml
        runner: callable(prompt, cwd) -> str (DI для тестов, вместо run_claude)

    Returns:
        Parsed health result dict или None при ошибке.
    """
    logger.info("health_monitor: starting health check")

    # 1. Build snapshot
    try:
        snapshot = await build_snapshot(db_path=db_path, config=config)
    except Exception as exc:
        logger.error("health_monitor: build_snapshot failed: %s", exc)
        return None

    # 2. Build prompt
    prompt = _build_health_prompt(snapshot)

    # 3. Run claude
    worker_dir = str(Path("workers") / "health_monitor")
    try:
        if runner is not None:
            raw_output = await runner(prompt, worker_dir)
        else:
            from supervisor.claude_runner import run_claude

            raw_output = await run_claude(prompt, cwd=worker_dir)
    except Exception as exc:
        logger.error("health_monitor: claude runner failed: %s", exc)
        return None

    # 4. Extract and validate JSON
    parsed = extract_json(raw_output)
    if parsed is None:
        logger.warning("health_monitor: failed to extract JSON from output")
        return None

    valid, err = validate_health_schema(parsed)
    if not valid:
        logger.warning("health_monitor: invalid health schema: %s", err)
        return None

    health = parsed.get("health", {})
    overall = health.get("overall", "GREEN")
    logger.info(
        "health_monitor: overall=%s confidence=%s", overall, parsed.get("confidence")
    )

    # 5. Execute auto_actions
    auto_actions = health.get("auto_actions", [])
    if auto_actions:
        action_results = await _execute_auto_actions(auto_actions, db_path=db_path)
        logger.info("health_monitor: auto_actions results: %s", action_results)

    # 6. Create tasks from create_tasks
    create_tasks_list = health.get("create_tasks", [])
    if create_tasks_list:
        task_ids = _create_health_tasks(create_tasks_list, db_path=db_path)
        logger.info("health_monitor: created %d task(s): %s", len(task_ids), task_ids)

    # 7. TG alert if RED or YELLOW
    if overall in ("RED", "YELLOW"):
        tg_alert = health.get("tg_alert", f"Health check: {overall}")
        alert_msg = f"[Health {overall}] {tg_alert}"
        try:
            await tg_handler.notify_owner(alert_msg)
            logger.info("health_monitor: sent TG alert: %s", overall)
        except Exception as exc:
            logger.error("health_monitor: TG alert failed: %s", exc)
    else:
        logger.info("health_monitor: GREEN — no alert needed")

    return parsed
