"""
supervisor/summarizer.py — daily digest for TG.

Sends a summary for the last 24 hours ONLY if there were tasks.
Does not write to DB directly (uses storage.db helpers).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from storage.db import get_conn, save_daily_summary, mark_summary_sent

logger = logging.getLogger(__name__)


async def build_daily_summary(
    db_path: Optional[str] = None, hours: int = 24
) -> Optional[str]:
    """
    Collect task statistics for the last `hours` hours.

    Returns:
        Formatted summary text, or None if no tasks were updated in the window.

    Stats collected:
    - Tasks done
    - Tasks blocked (blocked / requires_manual)
    - Tasks in progress (in_progress / running)
    - Top-3 error reasons (last_error_reason)
    """
    with get_conn(db_path) as conn:
        # Status counts for the time window
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM tasks
            WHERE updated_at >= datetime('now', ? || ' hours')
            GROUP BY status
            """,
            (str(-hours),),
        ).fetchall()

        if not rows:
            return None

        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = r["cnt"]

        done = counts.get("done", 0)
        blocked = counts.get("blocked", 0) + counts.get("requires_manual", 0)
        running = counts.get("in_progress", 0) + counts.get("running", 0)

        # Top-3 error reasons
        error_rows = conn.execute(
            """
            SELECT last_error_reason, COUNT(*) AS cnt
            FROM tasks
            WHERE last_error_reason IS NOT NULL
              AND updated_at >= datetime('now', ? || ' hours')
            GROUP BY last_error_reason
            ORDER BY cnt DESC
            LIMIT 3
            """,
            (str(-hours),),
        ).fetchall()

    # Build the text
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"\U0001f4ca \u0414\u0430\u0439\u0434\u0436\u0435\u0441\u0442 \u0437\u0430 {today}",
        f"\u2705 \u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u043e: {done}",
        f"\u26a0\ufe0f \u0417\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u043e: {blocked}",
        f"\U0001f504 \u0412 \u0440\u0430\u0431\u043e\u0442\u0435: {running}",
    ]

    if error_rows:
        lines.append("")
        lines.append("\u0422\u043e\u043f \u043e\u0448\u0438\u0431\u043e\u043a:")
        for er in error_rows:
            lines.append(f"- {er['last_error_reason']}: {er['cnt']}")

    return "\n".join(lines)


async def run_daily_summary(tg_handler, db_path: Optional[str] = None) -> None:
    """
    Build and send the daily digest via Telegram.

    If there were no tasks in the window, skip silently (no spam).
    """
    summary = await build_daily_summary(db_path)
    if summary:
        # Persist in DB before sending
        summary_id = save_daily_summary(summary)
        try:
            await tg_handler.notify_owner(summary)
            mark_summary_sent(summary_id)
            logger.info("run_daily_summary: sent (%d chars)", len(summary))
        except Exception as exc:
            logger.error("run_daily_summary: send failed: %s", exc)
    else:
        logger.info("run_daily_summary: no tasks in last 24h, skipping")
