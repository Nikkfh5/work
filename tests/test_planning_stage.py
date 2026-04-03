"""
tests/test_planning_stage.py — тесты для supervisor/stages/planning.py.

Запуск: pytest tests/test_planning_stage.py -v

Тестируем:
- planning_enabled=False → passthrough (no-op)
- simple task → passthrough (classify returns "simple")
- complex task → plan created, stored in DB, events emitted
- approved → status=running, plan_approved event
- revised → create_plan called again with feedback
- rejected → PlanRejected raised
- max revisions exhausted → PlanningError raised
- plan stored in DB (plan_text, plan_revision)
- lease renewal during _wait_for_plan_decision
"""

import json
import os
import threading

import pytest
from unittest.mock import AsyncMock, patch

from storage.db import get_conn, create_task
from supervisor.lease_manager import acquire_lease
from supervisor.pipeline import (
    PlanRejected,
    PlanningError,
    WorkerContext,
)
from supervisor.escalation import create_escalation, resolve_escalation
from supervisor.stages.planning import (
    _wait_for_clarification,
    _wait_for_plan_decision,
    planning_stage,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


VALID_PLAN = {
    "title": "Test Plan",
    "context": "Test context",
    "approach": "Test approach",
    "tasks": [
        {
            "step": 1,
            "description": "Do something",
            "files_to_modify": ["foo.py"],
            "changes": "change X",
            "verification": "run tests",
        }
    ],
    "risks": "none",
    "estimate": "1 step",
    "self_review": "looks good",
    "confidence": 80,
}


def _make_ctx(db_path, **overrides):
    """Создать WorkerContext с дефолтами для planning тестов."""
    defaults = dict(
        task_id="test-task-123",
        worker_id="job1_worker",
        task_description="test task",
        config={"supervisor": {}},
        tg_handler=AsyncMock(),
        db_path=db_path,
        worker_cfg={},
        job="job1",
        worker_dir="workers/job1_worker",
        planning_enabled=True,
        planning_config={
            "complexity_threshold": 200,
            "complexity_keywords": ["refactor", "migration"],
            "max_revisions": 3,
            "plan_timeout": 5,
            "poll_interval": 0.1,
        },
    )
    defaults.update(overrides)
    return WorkerContext(**defaults)


def _create_task_in_db(db_path, task_id="test-task-123", worker_id="job1_worker"):
    """Создать задачу в DB и захватить lease, возвращает token."""
    os.environ["DB_PATH"] = db_path
    tid = create_task(
        source="telegram",
        source_contact="@user",
        assigned_worker=worker_id,
        description="test task",
        client_contact="42",
    )
    # Перезаписываем ID на фиксированный для тестов
    with get_conn(db_path) as conn:
        conn.execute("UPDATE tasks SET id=? WHERE id=?", (task_id, tid))
    # Захватываем lease
    token = acquire_lease(task_id, worker_id, ttl=300, db_path=db_path)
    assert token is not None
    return token


def _write_decision_to_db(db_path, task_id, signal, delay=0.05):
    """Записать решение в partial_result задачи (в отдельном потоке с задержкой)."""

    def _writer():
        import time

        time.sleep(delay)
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE tasks SET partial_result=? WHERE id=?",
                (signal, task_id),
            )

    t = threading.Thread(target=_writer)
    t.start()
    return t


# ── 1. test_planning_disabled_passthrough ────────────────────────────────────


@pytest.mark.asyncio
async def test_planning_disabled_passthrough(db_path):
    """planning_enabled=False → no events emitted, classify not called."""
    ctx = _make_ctx(db_path, planning_enabled=False)

    with patch("supervisor.stages.planning.classify_complexity") as mock_classify:
        await planning_stage(ctx)

    mock_classify.assert_not_called()
    assert ctx.drain_events() == []
    # ctx.complexity not mutated
    assert ctx.complexity == ""


# ── 2. test_simple_task_passthrough ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_simple_task_passthrough(db_path):
    """Short description, no keywords → classify returns 'simple' → no events."""
    token = _create_task_in_db(db_path)
    ctx = _make_ctx(db_path, token=token, task_description="fix a typo")

    with (
        patch(
            "supervisor.stages.planning.needs_clarification",
            return_value=False,
        ),
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="simple",
        ) as mock_classify,
    ):
        await planning_stage(ctx)

    mock_classify.assert_awaited_once()
    assert ctx.complexity == "simple"
    assert ctx.drain_events() == []


# ── 3. test_complex_creates_plan_and_waits ───────────────────────────────────


@pytest.mark.asyncio
async def test_complex_creates_plan_and_waits(db_path):
    """complex → create_plan called, plan_created event emitted, approved → done."""
    task_id = "test-task-123"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(db_path, token=token, task_id=task_id)

    # Write approval signal to DB before the poll loop picks it up
    writer = _write_decision_to_db(db_path, task_id, "plan:approved", delay=0.15)

    with (
        patch(
            "supervisor.stages.planning.needs_clarification",
            return_value=False,
        ),
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="complex",
        ),
        patch(
            "supervisor.stages.planning.create_plan",
            new_callable=AsyncMock,
            return_value=VALID_PLAN,
        ) as mock_create_plan,
        patch(
            "supervisor.stages.planning.format_plan_for_tg",
            return_value="Plan text for TG",
        ),
        patch(
            "supervisor.stages.planning.renew_lease",
            return_value=True,
        ),
    ):
        await planning_stage(ctx)

    writer.join(timeout=2)

    mock_create_plan.assert_awaited_once()
    assert ctx.plan_text == json.dumps(VALID_PLAN, ensure_ascii=False)
    assert ctx.complexity == "complex"

    events = ctx.drain_events()
    event_types = [e["type"] for e in events]
    assert "plan_created" in event_types
    assert "plan_approved" in event_types


# ── 4. test_approved_continues_pipeline ──────────────────────────────────────


@pytest.mark.asyncio
async def test_approved_continues_pipeline(db_path):
    """After plan approved, task status in DB should be 'running'."""
    task_id = "test-task-456"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(db_path, token=token, task_id=task_id)

    writer = _write_decision_to_db(db_path, task_id, "plan:approved", delay=0.15)

    with (
        patch(
            "supervisor.stages.planning.needs_clarification",
            return_value=False,
        ),
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="complex",
        ),
        patch(
            "supervisor.stages.planning.create_plan",
            new_callable=AsyncMock,
            return_value=VALID_PLAN,
        ),
        patch(
            "supervisor.stages.planning.format_plan_for_tg",
            return_value="Plan text",
        ),
        patch(
            "supervisor.stages.planning.renew_lease",
            return_value=True,
        ),
    ):
        await planning_stage(ctx)

    writer.join(timeout=2)

    # Verify DB status is 'running' after approval
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row["status"] == "running"


# ── 5. test_revised_replans ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revised_replans(db_path):
    """_wait returns 'revised' first, then 'approved' → create_plan called twice."""
    task_id = "test-task-789"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(db_path, token=token, task_id=task_id)

    call_count = 0

    async def mock_wait(ctx_arg, timeout, poll_interval):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ("revised", "please add tests")
        return ("approved", "")

    with (
        patch(
            "supervisor.stages.planning.needs_clarification",
            return_value=False,
        ),
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="complex",
        ),
        patch(
            "supervisor.stages.planning.create_plan",
            new_callable=AsyncMock,
            return_value=VALID_PLAN,
        ) as mock_create_plan,
        patch(
            "supervisor.stages.planning.format_plan_for_tg",
            return_value="Plan text",
        ),
        patch(
            "supervisor.stages.planning._wait_for_plan_decision",
            side_effect=mock_wait,
        ),
    ):
        await planning_stage(ctx)

    assert mock_create_plan.await_count == 2
    # Second call should include feedback
    second_call_kwargs = mock_create_plan.call_args_list[1][1]
    assert second_call_kwargs.get("feedback") == "please add tests"
    assert second_call_kwargs.get("revision") == 1

    events = ctx.drain_events()
    event_types = [e["type"] for e in events]
    assert "plan_revision_requested" in event_types
    assert "plan_approved" in event_types


# ── 6. test_rejected_raises_plan_rejected ────────────────────────────────────


@pytest.mark.asyncio
async def test_rejected_raises_plan_rejected(db_path):
    """_wait returns 'rejected' → raises PlanRejected."""
    task_id = "test-task-rej"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(db_path, token=token, task_id=task_id)

    async def mock_wait(ctx_arg, timeout, poll_interval):
        return ("rejected", "")

    with (
        patch(
            "supervisor.stages.planning.needs_clarification",
            return_value=False,
        ),
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="complex",
        ),
        patch(
            "supervisor.stages.planning.create_plan",
            new_callable=AsyncMock,
            return_value=VALID_PLAN,
        ),
        patch(
            "supervisor.stages.planning.format_plan_for_tg",
            return_value="Plan text",
        ),
        patch(
            "supervisor.stages.planning._wait_for_plan_decision",
            side_effect=mock_wait,
        ),
    ):
        with pytest.raises(PlanRejected):
            await planning_stage(ctx)


# ── 7. test_max_revisions_exhausted ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_revisions_exhausted(db_path):
    """_wait always returns 'revised' → after max_revisions+1 loops → PlanningError."""
    task_id = "test-task-exh"
    token = _create_task_in_db(db_path, task_id=task_id)
    max_revisions = 2
    ctx = _make_ctx(
        db_path,
        token=token,
        task_id=task_id,
        planning_config={
            "complexity_threshold": 200,
            "complexity_keywords": ["refactor", "migration"],
            "max_revisions": max_revisions,
            "plan_timeout": 5,
            "poll_interval": 0.1,
        },
    )

    async def mock_wait(ctx_arg, timeout, poll_interval):
        return ("revised", "still not good")

    with (
        patch(
            "supervisor.stages.planning.needs_clarification",
            return_value=False,
        ),
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="complex",
        ),
        patch(
            "supervisor.stages.planning.create_plan",
            new_callable=AsyncMock,
            return_value=VALID_PLAN,
        ) as mock_create_plan,
        patch(
            "supervisor.stages.planning.format_plan_for_tg",
            return_value="Plan text",
        ),
        patch(
            "supervisor.stages.planning._wait_for_plan_decision",
            side_effect=mock_wait,
        ),
    ):
        with pytest.raises(PlanningError) as exc_info:
            await planning_stage(ctx)

    assert exc_info.value.reason == "plan_revision_exhausted"
    # create_plan called max_revisions + 1 times (0, 1, 2 → 3 calls)
    assert mock_create_plan.await_count == max_revisions + 1


# ── 8. test_plan_stored_in_db ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_stored_in_db(db_path):
    """After plan created, verify plan_text and plan_revision in DB."""
    task_id = "test-task-db"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(db_path, token=token, task_id=task_id)

    writer = _write_decision_to_db(db_path, task_id, "plan:approved", delay=0.15)

    with (
        patch(
            "supervisor.stages.planning.needs_clarification",
            return_value=False,
        ),
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="complex",
        ),
        patch(
            "supervisor.stages.planning.create_plan",
            new_callable=AsyncMock,
            return_value=VALID_PLAN,
        ),
        patch(
            "supervisor.stages.planning.format_plan_for_tg",
            return_value="Plan text",
        ),
        patch(
            "supervisor.stages.planning.renew_lease",
            return_value=True,
        ),
    ):
        await planning_stage(ctx)

    writer.join(timeout=2)

    # Check DB for plan_text and plan_revision
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT plan_text, plan_revision FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()

    expected_json = json.dumps(VALID_PLAN, ensure_ascii=False)
    assert row["plan_text"] == expected_json
    assert row["plan_revision"] == 0


# ── 9. test_wait_lease_renewal ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_lease_renewal(db_path):
    """Verify renew_lease is called during _wait_for_plan_decision."""
    task_id = "test-task-lease"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(
        db_path,
        token=token,
        task_id=task_id,
        lease_ttl=300,
        planning_config={
            "complexity_threshold": 200,
            "complexity_keywords": ["refactor", "migration"],
            "max_revisions": 3,
            "plan_timeout": 5,
            "poll_interval": 0.1,
        },
    )

    # Write decision after a short delay so the poll loop runs at least 2 cycles
    writer = _write_decision_to_db(db_path, task_id, "plan:approved", delay=0.3)

    with patch(
        "supervisor.stages.planning.renew_lease",
        return_value=True,
    ) as mock_renew:
        result = await _wait_for_plan_decision(ctx, timeout=5, poll_interval=0.1)

    writer.join(timeout=2)

    assert result == ("approved", "")
    # renew_lease should have been called at least once (likely 2-3 times)
    assert mock_renew.call_count >= 1
    # Verify it was called with the correct arguments
    call_kwargs = mock_renew.call_args
    assert call_kwargs[0][0] == task_id  # task_id
    assert call_kwargs[0][1] == "job1_worker"  # worker_id
    assert call_kwargs[0][2] == token  # token


# ── 10. test_clarification_triggered ───────────────────────────────────────


@pytest.mark.asyncio
async def test_clarification_triggered(db_path):
    """Vague description → needs_clarification=True → escalation created, TG notified."""
    task_id = "test-task-clar"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(
        db_path,
        token=token,
        task_id=task_id,
        task_description="авторизация",  # short, no action words
    )

    # Simulate owner resolving escalation during _wait_for_clarification
    async def mock_wait_clar(ctx_arg, esc_id, timeout, poll_interval):
        return "Нужна OAuth2 авторизация через Google"

    with (
        patch(
            "supervisor.stages.planning.generate_clarifying_questions",
            new_callable=AsyncMock,
            return_value="1. Что?\n2. Какие файлы?\n3. Результат?",
        ) as mock_gen,
        patch(
            "supervisor.stages.planning._wait_for_clarification",
            side_effect=mock_wait_clar,
        ),
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="simple",
        ),
    ):
        await planning_stage(ctx)

    # generate_clarifying_questions was called
    mock_gen.assert_awaited_once()

    # TG handler was notified
    ctx.tg_handler.notify_owner.assert_awaited_once()
    notify_text = ctx.tg_handler.notify_owner.call_args[0][0]
    assert "уточнения" in notify_text.lower()

    # Description was enriched
    assert "Уточнение от владельца" in ctx.task_description
    assert "OAuth2" in ctx.task_description

    # Description updated in DB
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT description FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert "OAuth2" in row["description"]


# ── 11. test_clarification_skipped_for_clear_description ───────────────────


@pytest.mark.asyncio
async def test_clarification_skipped_for_clear_description(db_path):
    """Clear description with action word → no clarification needed."""
    task_id = "test-task-clear"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(
        db_path,
        token=token,
        task_id=task_id,
        task_description="добавь эндпоинт /api/users для получения списка пользователей",
    )

    with (
        patch(
            "supervisor.stages.planning.generate_clarifying_questions",
            new_callable=AsyncMock,
        ) as mock_gen,
        patch(
            "supervisor.stages.planning.classify_complexity",
            new_callable=AsyncMock,
            return_value="simple",
        ),
    ):
        await planning_stage(ctx)

    # generate_clarifying_questions should NOT have been called
    mock_gen.assert_not_awaited()


# ── 12. test_wait_for_clarification_resolved ───────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_clarification_resolved(db_path):
    """Escalation resolved → returns response text."""
    task_id = "test-task-wfc"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(db_path, token=token, task_id=task_id)

    # Create escalation
    esc_id = create_escalation(
        task_id=task_id,
        escalation_type="clarification",
        question="Уточни?",
        context="test",
        db_path=db_path,
    )

    # Resolve it in a background thread after a short delay
    def _resolve():
        import time
        time.sleep(0.15)
        resolve_escalation(esc_id, "Нужна OAuth2", db_path)

    t = threading.Thread(target=_resolve)
    t.start()

    with patch(
        "supervisor.stages.planning.renew_lease",
        return_value=True,
    ):
        result = await _wait_for_clarification(
            ctx, esc_id, timeout=5, poll_interval=0.1
        )

    t.join(timeout=2)
    assert result == "Нужна OAuth2"


# ── 13. test_wait_for_clarification_timeout ────────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_clarification_timeout(db_path):
    """No resolution within timeout → StageError raised."""
    from supervisor.pipeline import StageError

    task_id = "test-task-wfc-to"
    token = _create_task_in_db(db_path, task_id=task_id)
    ctx = _make_ctx(db_path, token=token, task_id=task_id)

    # Create escalation but never resolve it
    esc_id = create_escalation(
        task_id=task_id,
        escalation_type="clarification",
        question="Уточни?",
        context="test",
        db_path=db_path,
    )

    with patch(
        "supervisor.stages.planning.renew_lease",
        return_value=True,
    ):
        with pytest.raises(StageError) as exc_info:
            await _wait_for_clarification(
                ctx, esc_id, timeout=0.2, poll_interval=0.1
            )

    assert exc_info.value.reason == "clarification_timeout"
