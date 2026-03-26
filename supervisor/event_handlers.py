"""
supervisor/event_handlers.py — Event dispatch for notifications.

Single file that maps event types emitted by stages (via ctx.emit)
to notification channels (currently Telegram via tg_handler).

Adding a new channel (Slack, webhook, etc.) requires changes only here,
not in every stage file.

Event types:
  task_done           -- task completed (confidence, notes)
  task_failed         -- task failed (reason, message)
  task_blocked        -- task blocked, needs human input (question)
  review_approved     -- reviewer approved the result (message)
  review_needs_changes -- reviewer requested changes (iteration, feedback)
  escalation_created  -- escalation created (question)
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supervisor.pipeline import WorkerContext

logger = logging.getLogger(__name__)


async def handle_events(ctx: "WorkerContext") -> None:
    """Drain and dispatch all accumulated events from ctx."""
    for event in ctx.drain_events():
        await _dispatch_event(ctx, event)


async def _dispatch_event(ctx: "WorkerContext", event: dict) -> None:
    """Route a single event to the appropriate notification channel."""
    event_type = event["type"]
    data = event["data"]

    if event_type == "task_done":
        confidence = data.get("confidence", "?")
        notes = data.get("notes", "")
        await ctx.tg_handler.notify_owner(
            f"task#{ctx.task_id[:8]}: DONE \u2713 (confidence={confidence})\n"
            f"{str(notes)[:200]}"
        )

    elif event_type == "task_failed":
        message = data.get("message", data.get("reason", "error"))
        await ctx.tg_handler.notify_owner(
            f"\u26a0\ufe0f task#{ctx.task_id[:8]}: {message}\n"
            f"\u041f\u043e\u0432\u0442\u043e\u0440\u0438: /retry {ctx.task_id[:8]}"
        )

    elif event_type == "task_blocked":
        question = data.get("question", "")
        await ctx.tg_handler.notify_owner(
            f"task#{ctx.task_id[:8]}: BLOCKED. {str(question)[:200]}\n"
            f"\u041f\u043e\u0432\u0442\u043e\u0440\u0438: /retry {ctx.task_id[:8]}"
        )

    elif event_type == "review_approved":
        message = data.get("message", "APPROVED \u2713")
        await ctx.tg_handler.notify_owner(f"task#{ctx.task_id[:8]}: {message}")

    elif event_type == "review_needs_changes":
        iteration = data.get("iteration", "?")
        feedback = data.get("feedback", "")
        await ctx.tg_handler.notify_owner(
            f"task#{ctx.task_id[:8]}: NEEDS_CHANGES (iter={iteration})\n"
            f"{str(feedback)[:200]}"
        )

    elif event_type == "escalation_created":
        question = data.get("question", "")
        await ctx.tg_handler.notify_owner(
            f"task#{ctx.task_id[:8]}: \u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f "
            f"\u043f\u043e\u043c\u043e\u0449\u044c. {str(question)[:200]}\n"
            f"\u041f\u043e\u0432\u0442\u043e\u0440\u0438: /retry {ctx.task_id[:8]}"
        )

    else:
        logger.warning("Unknown event type: %s", event_type)
