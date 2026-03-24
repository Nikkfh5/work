"""
tests/test_escalation.py — тесты для supervisor/escalation.py.

8 тестов:
  1. supervisor_reasoning: confident answer
  2. supervisor_reasoning: error fallback
  3. handle_worker_blocked: auto-resolve
  4. handle_worker_blocked: escalate
  5. create + resolve escalation lifecycle
  6. route_owner_reply: matches open escalation
  7. route_owner_reply: no open escalation
  8. handle_pending_approval: TG notify format
"""

import pytest
from unittest.mock import AsyncMock, patch

from supervisor.escalation import (
    supervisor_reasoning,
    handle_worker_blocked,
    handle_pending_approval,
    create_escalation,
    resolve_escalation,
    get_open_escalation,
    route_owner_reply,
)


# ── 1. supervisor_reasoning: confident answer ────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_reasoning_returns_answer(db_path):
    """run_claude returns confident JSON -> dict with answer and confidence."""
    mock_response = (
        "<<<JSON>>>\n"
        '{"answer": "Используй endpoint /api/v2", "confidence": 85}\n'
        "<<<END>>>"
    )
    with patch(
        "supervisor.claude_runner.run_claude",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        result = await supervisor_reasoning(
            question="Какой API endpoint использовать?",
            task_context="Интеграция с внешним сервисом",
            db_path=db_path,
        )

    assert result["answer"] == "Используй endpoint /api/v2"
    assert result["confidence"] == 85


# ── 2. supervisor_reasoning: error fallback ──────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_reasoning_error_fallback(db_path):
    """run_claude throws -> fallback {"answer": "", "confidence": 0}."""
    with patch(
        "supervisor.claude_runner.run_claude",
        new_callable=AsyncMock,
        side_effect=RuntimeError("connection lost"),
    ):
        result = await supervisor_reasoning(
            question="Что делать?",
            task_context="контекст",
            db_path=db_path,
        )

    assert result["answer"] == ""
    assert result["confidence"] == 0


# ── 3. handle_worker_blocked: auto-resolve ───────────────────────────────────


@pytest.mark.asyncio
async def test_handle_blocked_auto_resolve(db_path, mock_tg_handler):
    """Reasoning confident -> return 'directive:...'."""
    task = _make_task(db_path)
    worker_output = {"status": "blocked", "question": "Какой формат?", "result": {}}
    config = {"supervisor": {"reasoning_threshold": 70}}

    mock_reasoning_response = (
        '<<<JSON>>>\n{"answer": "Используй JSON формат", "confidence": 90}\n<<<END>>>'
    )
    with patch(
        "supervisor.claude_runner.run_claude",
        new_callable=AsyncMock,
        return_value=mock_reasoning_response,
    ):
        result = await handle_worker_blocked(
            task=task,
            worker_output=worker_output,
            config=config,
            tg_handler=mock_tg_handler,
            db_path=db_path,
        )

    assert result == "directive:Используй JSON формат"
    # TG не вызывается при auto-resolve
    mock_tg_handler.notify_owner.assert_not_called()


# ── 4. handle_worker_blocked: escalate ───────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_blocked_escalate(db_path, mock_tg_handler):
    """Reasoning not confident -> 'escalated' + escalation in DB + TG notify."""
    task = _make_task(db_path)
    worker_output = {
        "status": "blocked",
        "question": "Нужен доступ к API",
        "result": {},
    }
    config = {"supervisor": {"reasoning_threshold": 70}}

    mock_reasoning_response = (
        '<<<JSON>>>\n{"answer": "Не уверен", "confidence": 30}\n<<<END>>>'
    )
    with patch(
        "supervisor.claude_runner.run_claude",
        new_callable=AsyncMock,
        return_value=mock_reasoning_response,
    ):
        result = await handle_worker_blocked(
            task=task,
            worker_output=worker_output,
            config=config,
            tg_handler=mock_tg_handler,
            db_path=db_path,
        )

    assert result == "escalated"

    # Проверяем что эскалация создана в DB
    esc = get_open_escalation(task["id"], db_path)
    assert esc is not None
    assert esc["task_id"] == task["id"]
    assert esc["reason"] == "worker_blocked"
    assert esc["question"] == "Нужен доступ к API"

    # TG уведомление отправлено
    mock_tg_handler.notify_owner.assert_called_once()
    call_text = mock_tg_handler.notify_owner.call_args[0][0]
    assert task["id"][:8] in call_text
    assert "Нужен доступ к API" in call_text


# ── 5. create + resolve escalation lifecycle ─────────────────────────────────


def test_create_and_resolve_escalation(db_path):
    """create -> get_open -> resolve -> get_open returns None."""
    task = _make_task(db_path)
    task_id = task["id"]

    # Create
    esc_id = create_escalation(
        task_id=task_id,
        escalation_type="worker_blocked",
        question="Какой формат данных?",
        context="контекст задачи",
        db_path=db_path,
        uuid_fn=lambda: "esc-fixed-id-001",
    )
    assert esc_id == "esc-fixed-id-001"

    # get_open — should find it
    esc = get_open_escalation(task_id, db_path)
    assert esc is not None
    assert esc["id"] == "esc-fixed-id-001"
    assert esc["question"] == "Какой формат данных?"
    assert esc["resolved"] == 0

    # Resolve
    resolve_escalation("esc-fixed-id-001", "Используй CSV", db_path)

    # get_open — should be None now
    esc_after = get_open_escalation(task_id, db_path)
    assert esc_after is None


# ── 6. route_owner_reply: matches open escalation ───────────────────────────


@pytest.mark.asyncio
async def test_route_owner_reply_matches(db_path, mock_tg_handler):
    """Open escalation -> resolve + return task_id."""
    task = _make_task(db_path)
    task_id = task["id"]

    create_escalation(
        task_id=task_id,
        escalation_type="worker_blocked",
        question="Нужен ответ",
        db_path=db_path,
    )

    result = await route_owner_reply(
        "Вот ответ: используй v2", mock_tg_handler, db_path
    )

    assert result == task_id

    # Escalation should be resolved
    esc = get_open_escalation(task_id, db_path)
    assert esc is None


# ── 7. route_owner_reply: no open escalation ────────────────────────────────


@pytest.mark.asyncio
async def test_route_owner_reply_no_match(db_path, mock_tg_handler):
    """No open escalation -> None."""
    result = await route_owner_reply("какой-то текст", mock_tg_handler, db_path)
    assert result is None


# ── 8. handle_pending_approval: TG notify format ────────────────────────────


@pytest.mark.asyncio
async def test_handle_pending_approval(db_path, mock_tg_handler):
    """TG notification format includes task id and approve/reject commands."""
    task = _make_task(db_path)

    await handle_pending_approval(task, mock_tg_handler, db_path)

    mock_tg_handler.notify_owner.assert_called_once()
    call_text = mock_tg_handler.notify_owner.call_args[0][0]

    # Check format
    assert task["id"][:8] in call_text
    assert "/approve" in call_text
    assert "/reject" in call_text
    assert "одобрение" in call_text.lower() or "требуется" in call_text.lower()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_task(db_path: str) -> dict:
    """Создать задачу в DB и вернуть dict."""
    from storage.db import create_task, get_task

    task_id = create_task(
        source="telegram",
        source_contact="@test",
        assigned_worker="test_worker",
        description="Test task description for escalation",
        client_contact="12345",
    )
    return get_task(task_id)
