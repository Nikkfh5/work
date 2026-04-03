"""
supervisor/stages/planning.py — Stage: complexity check + planning + TG approval.

Условный stage между prepare_stage и execute_stage.
Если задача простая → passthrough (no-op).
Если сложная → создаёт план, отправляет в TG, ждёт одобрения.

Мутирует ctx: complexity, plan_text, plan_revision.

Сигнализация решения: TG-команды пишут в partial_result (не меняют status),
planning_stage поллит partial_result + продлевает lease.

Инварианты:
- Lease продлевается каждый poll-цикл (не устаревает при ожидании)
- Max revisions ограничены конфигом
- На reject → PlanRejected (StageError) → pipeline handles cleanup
"""

import asyncio
import logging
from typing import Optional

from storage.db import get_conn
from supervisor.lease_manager import renew_lease
from supervisor.pipeline import PlanRejected, PlanningError, StageError, WorkerContext
from supervisor.planner import (
    classify_complexity,
    create_plan,
    format_plan_for_tg,
    generate_clarifying_questions,
    needs_clarification,
)

logger = logging.getLogger(__name__)

E_PLANNING_FAILED = "planning_failed"
E_PLAN_REVISION_EXHAUSTED = "plan_revision_exhausted"
E_PLAN_TIMEOUT = "plan_timeout"
E_CLARIFICATION_TIMEOUT = "clarification_timeout"


async def planning_stage(ctx: WorkerContext) -> None:
    """
    Условный planning stage.

    1. Если planning_enabled=False → return (no-op)
    2. classify_complexity()
    3. Если "simple" → return (no-op, pipeline continues)
    4. Если "complex":
       a. Создать план через super-agent
       b. Сохранить план в DB
       c. Отправить в TG через event
       d. Ждать решения владельца (poll loop)
       e. По решению: continue / re-plan / raise PlanRejected
    """
    if not ctx.planning_enabled:
        return

    # ── Clarification check ──────────────────────────────────────────────
    if needs_clarification(ctx.task_description, ctx.planning_config):
        logger.info("planning_stage: task needs clarification task_id=%s", ctx.task_id)

        # Update status
        _update_task_field(ctx.task_id, "status", "planning", ctx.db_path)

        # Generate questions
        questions = await generate_clarifying_questions(
            ctx.task_description, runner=ctx.runner
        )

        # Use escalation mechanism to ask owner
        from supervisor.escalation import create_escalation

        esc_id = create_escalation(
            task_id=ctx.task_id,
            escalation_type="clarification",
            question=questions,
            context=ctx.task_description[:500],
            db_path=ctx.db_path,
        )

        # Notify via TG
        short_id = ctx.task_id[:8]
        await ctx.tg_handler.notify_owner(
            f"task#{short_id}: Задача требует уточнения.\n\n"
            f"Описание: {ctx.task_description[:200]}\n\n"
            f"{questions}\n\n"
            f"Ответь текстом или /cancel {short_id}"
        )

        # Wait for owner's clarification response (poll escalation resolution)
        timeout = int(ctx.planning_config.get("plan_timeout", 86400))
        poll_interval = int(ctx.planning_config.get("poll_interval", 10))

        clarification = await _wait_for_clarification(
            ctx, esc_id, timeout=timeout, poll_interval=poll_interval
        )

        if clarification:
            # Enrich task description
            ctx.task_description = (
                f"{ctx.task_description}\n\n"
                f"--- Уточнение от владельца ---\n{clarification}"
            )
            # Update description in DB
            with get_conn(ctx.db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET description=?, updated_at=datetime('now') WHERE id=?",
                    (ctx.task_description, ctx.task_id),
                )
            logger.info("planning_stage: clarification received task_id=%s", ctx.task_id)

    # ── Complexity classification ────────────────────────────────────────
    # Классификация сложности
    runner = ctx.runner
    complexity = await classify_complexity(
        ctx.task_description,
        ctx.planning_config,
        runner=runner,
    )
    ctx.complexity = complexity

    # Сохраняем complexity в DB
    _update_task_field(ctx.task_id, "complexity", complexity, ctx.db_path)

    if complexity == "simple":
        logger.info("planning_stage: simple task, skipping task_id=%s", ctx.task_id)
        return

    # Complex task: входим в planning loop
    logger.info("planning_stage: complex task, entering planning task_id=%s", ctx.task_id)

    max_revisions = int(ctx.planning_config.get("max_revisions", 3))
    feedback = ""

    for revision in range(max_revisions + 1):
        ctx.plan_revision = revision

        # Обновляем status → planning
        _update_task_field(ctx.task_id, "status", "planning", ctx.db_path)

        # Создаём план
        plan = await create_plan(
            task_description=ctx.task_description,
            task_id=ctx.task_id,
            config=ctx.config,
            feedback=feedback,
            revision=revision,
            db_path=ctx.db_path,
            runner=runner,
        )

        if not plan:
            raise PlanningError(
                reason=E_PLANNING_FAILED,
                message=f"Failed to create plan (revision {revision})",
            )

        # Сохраняем план в DB и ctx
        import json

        plan_json = json.dumps(plan, ensure_ascii=False)
        ctx.plan_text = plan_json

        with get_conn(ctx.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET plan_text=?, plan_revision=?, "
                "status='plan_review', updated_at=datetime('now') "
                "WHERE id=?",
                (plan_json, revision, ctx.task_id),
            )

        # Формат для TG и отправка
        tg_text = format_plan_for_tg(plan, ctx.task_id)
        ctx.emit("plan_created", plan_text=tg_text)

        logger.info(
            "planning_stage: plan sent for review task_id=%s revision=%d",
            ctx.task_id,
            revision,
        )

        # Ждём решения владельца
        timeout = int(ctx.planning_config.get("plan_timeout", 86400))
        poll_interval = int(ctx.planning_config.get("poll_interval", 10))

        decision, new_feedback = await _wait_for_plan_decision(
            ctx,
            timeout=timeout,
            poll_interval=poll_interval,
        )

        if decision == "approved":
            # Очищаем signal, восстанавливаем status → running
            with get_conn(ctx.db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET partial_result=NULL, status='running', "
                    "updated_at=datetime('now') WHERE id=?",
                    (ctx.task_id,),
                )
            ctx.emit("plan_approved", task_id=ctx.task_id)
            logger.info("planning_stage: plan approved task_id=%s", ctx.task_id)
            return

        elif decision == "revised":
            feedback = new_feedback
            # Очищаем signal для следующей итерации
            _update_task_field(ctx.task_id, "partial_result", None, ctx.db_path)
            ctx.emit(
                "plan_revision_requested",
                revision=revision + 1,
                feedback=feedback[:200],
            )
            logger.info(
                "planning_stage: revision requested task_id=%s revision=%d",
                ctx.task_id,
                revision + 1,
            )
            continue

        elif decision == "rejected":
            logger.info("planning_stage: plan rejected task_id=%s", ctx.task_id)
            raise PlanRejected()

    # Max revisions exhausted
    raise PlanningError(
        reason=E_PLAN_REVISION_EXHAUSTED,
        message=f"Plan revision limit ({max_revisions}) exhausted",
    )


async def _wait_for_plan_decision(
    ctx: WorkerContext,
    timeout: int = 86400,
    poll_interval: int = 10,
) -> tuple[str, str]:
    """
    Поллить DB для решения по плану, продлевая lease каждый цикл.

    Returns:
        (decision, feedback) где decision: "approved"|"revised"|"rejected"
        и feedback: комментарии владельца (пусто если approved/rejected).

    Raises:
        StageError: если lease потерян или таймаут.
    """
    elapsed = 0

    while elapsed < timeout:
        # Продлеваем lease чтобы не устарел
        renewed = renew_lease(
            ctx.task_id,
            ctx.worker_id,
            ctx.token,
            ttl=max(ctx.lease_ttl, poll_interval * 3),
            db_path=ctx.db_path,
        )
        if not renewed:
            logger.warning(
                "planning: lease lost during plan_review task_id=%s",
                ctx.task_id,
            )
            raise StageError(
                reason="lease_stale",
                message="Lost lease during plan review",
            )

        # Проверяем signal в partial_result + статус задачи
        with get_conn(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT partial_result, status FROM tasks WHERE id=?",
                (ctx.task_id,),
            ).fetchone()

        if not row:
            raise StageError(reason="task_not_found", message="Task disappeared during plan review")

        # Если задачу отменили через /cancel — выходим
        if row["status"] == "cancelled":
            return ("rejected", "")

        if row["partial_result"] and str(row["partial_result"]).startswith("plan:"):
            signal = str(row["partial_result"])
            if signal == "plan:approved":
                return ("approved", "")
            elif signal.startswith("plan:revised:"):
                fb = signal[len("plan:revised:"):]
                return ("revised", fb)
            elif signal == "plan:rejected":
                return ("rejected", "")

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise StageError(
        reason=E_PLAN_TIMEOUT,
        message="Plan approval timed out",
    )


async def _wait_for_clarification(
    ctx: WorkerContext,
    escalation_id: str,
    timeout: int = 86400,
    poll_interval: int = 10,
) -> Optional[str]:
    """
    Poll for escalation resolution (owner's clarification response).

    Renews lease every cycle to prevent stale-lease expiration.

    Args:
        ctx: WorkerContext (task_id, worker_id, token, lease_ttl, db_path)
        escalation_id: ID созданной эскалации
        timeout: максимальное время ожидания (секунды)
        poll_interval: интервал проверки (секунды)

    Returns:
        Текст ответа владельца, или None если эскалация удалена без response.

    Raises:
        StageError: если lease потерян или таймаут.
    """
    from supervisor.escalation import get_open_escalation

    elapsed = 0
    while elapsed < timeout:
        # Renew lease
        renewed = renew_lease(
            ctx.task_id,
            ctx.worker_id,
            ctx.token,
            ttl=max(ctx.lease_ttl, poll_interval * 3),
            db_path=ctx.db_path,
        )
        if not renewed:
            raise StageError(
                reason="lease_stale",
                message="Lost lease during clarification",
            )

        # Check if escalation is resolved
        esc = get_open_escalation(ctx.task_id, ctx.db_path)
        if esc is None:
            # Escalation was resolved — get the response
            with get_conn(ctx.db_path) as conn:
                row = conn.execute(
                    "SELECT response FROM escalations WHERE id=?",
                    (escalation_id,),
                ).fetchone()
            return row["response"] if row else None

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise StageError(
        reason=E_CLARIFICATION_TIMEOUT,
        message="Clarification timed out",
    )


def _update_task_field(
    task_id: str,
    field: str,
    value,
    db_path: Optional[str] = None,
) -> None:
    """Обновить одно поле задачи в DB. Только для безопасных полей.

    При обновлении status — проверяет допустимость перехода через check_transition.
    """
    from supervisor.lease_manager import check_transition

    allowed_fields = {"complexity", "status", "partial_result", "plan_text", "plan_revision"}
    if field not in allowed_fields:
        raise ValueError(f"Field {field!r} not in allowed set")

    with get_conn(db_path) as conn:
        if field == "status" and value is not None:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row:
                current = row["status"]
                if not check_transition(current, value):
                    logger.warning(
                        "_update_task_field: invalid transition %s -> %s task_id=%s",
                        current, value, task_id,
                    )
                    return

        conn.execute(
            f"UPDATE tasks SET {field}=?, updated_at=datetime('now') WHERE id=?",
            (value, task_id),
        )
