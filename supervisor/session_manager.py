"""
supervisor/session_manager.py — Session checkpoint management for long tasks.

Saves/loads checkpoints when Claude CLI session approaches context limits.
Enables task resumption with a shorter, summarized prompt.

Inspired by Gas Town checkpoint/handoff mechanism.

Инварианты:
- Нет shell=True
- Все DB операции через get_conn
- Checkpoint data — JSON, не raw content
"""

import logging
from typing import Optional

from storage.db import get_conn

logger = logging.getLogger(__name__)

# Default threshold: refresh session when 80% of context window is used
DEFAULT_CONTEXT_THRESHOLD = 0.8
# Default threshold: refresh when 90% of max output tokens used
DEFAULT_OUTPUT_THRESHOLD = 0.9


def save_checkpoint(
    task_id: str,
    worker_id: str,
    phase: str,
    attempt: int,
    metrics: dict,
    progress_summary: str,
    saved_context: str = "",
    db_path: Optional[str] = None,
) -> int:
    """
    Save a session checkpoint for later resumption.

    Args:
        task_id: ID задачи
        worker_id: ID воркера
        phase: "worker" | "reviewer"
        attempt: номер текущей попытки
        metrics: dict from cost_tracker.extract_metrics()
        progress_summary: краткое описание что сделано (из stdout)
        saved_context: сохранённый контекст (prompt prefix для resume)
        db_path: путь к БД

    Returns:
        checkpoint ID
    """
    input_tokens = metrics.get("input_tokens", 0) + metrics.get("cache_creation_tokens", 0)
    output_tokens = metrics.get("output_tokens", 0)
    cost_usd = metrics.get("cost_usd", 0.0)

    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO session_checkpoints
               (task_id, worker_id, phase, attempt,
                input_tokens, output_tokens, cost_usd,
                progress_summary, saved_context)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                worker_id,
                phase,
                attempt,
                input_tokens,
                output_tokens,
                cost_usd,
                progress_summary[:2000],
                saved_context[:5000],
            ),
        )
        checkpoint_id = cursor.lastrowid

    logger.info(
        "save_checkpoint: id=%d task_id=%s tokens=%d cost=$%.4f",
        checkpoint_id,
        task_id,
        input_tokens,
        cost_usd,
    )
    return checkpoint_id


def load_checkpoint(
    task_id: str,
    db_path: Optional[str] = None,
) -> Optional[dict]:
    """
    Load the latest unresolved checkpoint for a task.

    Returns:
        dict with checkpoint data, or None if no checkpoint exists.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM session_checkpoints
               WHERE task_id=? AND resumed=0
               ORDER BY checkpoint_at DESC LIMIT 1""",
            (task_id,),
        ).fetchone()

    if not row:
        return None

    checkpoint = dict(row)
    logger.info(
        "load_checkpoint: found checkpoint id=%d task_id=%s",
        checkpoint["id"],
        task_id,
    )
    return checkpoint


def mark_resumed(
    checkpoint_id: int,
    db_path: Optional[str] = None,
) -> None:
    """Mark checkpoint as resumed (so it won't be loaded again)."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE session_checkpoints SET resumed=1 WHERE id=?",
            (checkpoint_id,),
        )
    logger.info("mark_resumed: checkpoint_id=%d", checkpoint_id)


def should_refresh_session(
    metrics: dict,
    context_threshold: float = DEFAULT_CONTEXT_THRESHOLD,
    output_threshold: float = DEFAULT_OUTPUT_THRESHOLD,
) -> bool:
    """
    Check if session needs refresh based on token usage from metrics.

    Uses context_window and max_output_tokens from Claude CLI JSON response
    to determine if we're approaching limits.

    Args:
        metrics: dict from cost_tracker.extract_metrics()
        context_threshold: fraction of context window to trigger (default 0.8)
        output_threshold: fraction of max output tokens to trigger (default 0.9)

    Returns:
        True if session should be refreshed.
    """
    # Check input context usage
    ctx_window = metrics.get("context_window", 0)
    if ctx_window > 0:
        used_input = (
            metrics.get("input_tokens", 0)
            + metrics.get("cache_creation_tokens", 0)
            + metrics.get("cache_read_tokens", 0)
        )
        if used_input > ctx_window * context_threshold:
            logger.info(
                "should_refresh_session: context usage %.1f%% > %.0f%% threshold",
                used_input / ctx_window * 100,
                context_threshold * 100,
            )
            return True

    # Check output token usage
    max_output = metrics.get("max_output_tokens", 0)
    if max_output > 0:
        used_output = metrics.get("output_tokens", 0)
        if used_output > max_output * output_threshold:
            logger.info(
                "should_refresh_session: output usage %.1f%% > %.0f%% threshold",
                used_output / max_output * 100,
                output_threshold * 100,
            )
            return True

    return False


def build_resumed_prompt(
    original_description: str,
    checkpoint: dict,
    plan_text: str = "",
) -> str:
    """
    Build a shorter prompt for a resumed session.

    Instead of the full accumulated context, provides a summary
    of previous progress + remaining work.

    Args:
        original_description: оригинальное описание задачи
        checkpoint: dict from load_checkpoint()
        plan_text: утверждённый план (если есть)

    Returns:
        Condensed prompt for resumed session.
    """
    progress = checkpoint.get("progress_summary", "")
    tokens_used = checkpoint.get("input_tokens", 0) + checkpoint.get("output_tokens", 0)
    attempt = checkpoint.get("attempt", 0)

    plan_section = ""
    if plan_text:
        plan_section = (
            "\n--- УТВЕРЖДЁННЫЙ ПЛАН ---\n"
            f"{plan_text[:2000]}\n"
            "--- КОНЕЦ ПЛАНА ---\n\n"
        )

    return f"""\
[SESSION RESUMED — attempt {attempt + 1}]

Задача: {original_description[:1000]}

{plan_section}Предыдущий прогресс (tokens used: {tokens_used}):
{progress[:1000]}

Продолжи с того места где остановился. НЕ переделывай то что уже сделано.
Сфокусируйся на оставшейся работе.

<<<JSON>>>
{{
  "status": "done",
  "confidence": <0-100>,
  "result": {{
    "repos": [],
    "notes": "<что сделано в этой сессии + суммарный прогресс>"
  }},
  "question": null
}}
<<<END>>>
"""


def extract_progress_summary(stdout: str) -> str:
    """
    Extract a brief progress summary from worker stdout.

    Tries to find structured output (JSON notes) or falls back to last lines.
    """
    # Try to extract from JSON result
    try:
        # Look for <<<JSON>>>...<<<END>>> pattern
        from supervisor.json_guard import extract_json

        parsed = extract_json(stdout)
        if parsed:
            notes = parsed.get("result", {}).get("notes", "")
            if notes:
                return notes[:500]
    except Exception:
        pass

    # Fallback: last 500 chars of stdout
    if stdout:
        return stdout[-500:].strip()
    return ""
