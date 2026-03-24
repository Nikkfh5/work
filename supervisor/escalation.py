"""
supervisor/escalation.py — обработка эскалаций.

5 сценариев:
  1. blocked/low confidence → supervisor_reasoning → auto-resolve или TG
  2. blocked с partial → то же + partial контекст
  3. crash/timeout → уже в main.py (retry + requires_manual)
  4. review exhausted → уже в main.py (requires_manual)
  5. git push fail → уже в main.py (requires_manual)

pending_approval: health_monitor tasks → /approve /reject через TG

Инварианты:
- НЕ запускает shell=True
- Все логи через logger
- DI: db_path, uuid_fn как параметры
- import run_claude ВНУТРИ функции (lazy import)
"""

import logging
import uuid
from typing import Optional

from storage.db import get_conn
from supervisor.json_guard import extract_json

logger = logging.getLogger(__name__)

# Пороговое значение confidence по умолчанию для supervisor reasoning
DEFAULT_SUPERVISOR_THRESHOLD = 70


async def supervisor_reasoning(
    question: str,
    task_context: str,
    db_path: Optional[str] = None,
) -> dict:
    """
    Запросить у Claude reasoning по вопросу заблокированного воркера.

    Вызывает run_claude с промптом, парсит JSON-ответ.

    Args:
        question: вопрос воркера
        task_context: контекст задачи (описание, partial result и т.д.)
        db_path: путь к БД (не используется напрямую, для совместимости)

    Returns:
        dict с ключами "answer" (str) и "confidence" (int 0-100).
        При ошибке: {"answer": "", "confidence": 0}
    """
    from supervisor.claude_runner import run_claude

    prompt = (
        f"Воркер заблокирован: {question}\n"
        f"Контекст задачи: {task_context}\n\n"
        "Проанализируй ситуацию и ответь JSON:\n"
        "<<<JSON>>>\n"
        '{"answer": "<твой ответ/решение>", "confidence": <0-100>}\n'
        "<<<END>>>"
    )

    try:
        raw = await run_claude(prompt, cwd=None, timeout=120)
    except Exception as exc:
        logger.error("supervisor_reasoning: run_claude failed: %s", exc)
        return {"answer": "", "confidence": 0}

    parsed = extract_json(raw)
    if not parsed:
        logger.warning("supervisor_reasoning: no valid JSON in response")
        return {"answer": "", "confidence": 0}

    answer = parsed.get("answer", "")
    confidence = parsed.get("confidence", 0)

    if not isinstance(answer, str):
        answer = str(answer) if answer else ""
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 100):
        logger.warning(
            "supervisor_reasoning: invalid confidence=%r, defaulting to 0",
            confidence,
        )
        confidence = 0

    return {"answer": answer, "confidence": int(confidence)}


async def handle_worker_blocked(
    task: dict,
    worker_output: dict,
    config: dict,
    tg_handler,
    db_path: Optional[str] = None,
) -> str:
    """
    Обработать blocked-статус воркера: попытка auto-resolve через supervisor_reasoning.

    Args:
        task: dict задачи из DB (id, description, assigned_worker, ...)
        worker_output: распарсенный JSON от воркера (status, question, result, ...)
        config: полный конфиг (agents.yaml)
        tg_handler: TelegramHandler для уведомлений
        db_path: путь к БД

    Returns:
        "directive:{answer}" — если reasoning уверен (auto-resolve)
        "escalated" — если reasoning не уверен (создана эскалация + TG)
    """
    task_id = task["id"]
    question = worker_output.get("question") or "воркер заблокирован без уточнений"

    # Собираем контекст задачи
    task_context = task.get("description", "")
    partial = task.get("partial_result") or worker_output.get("result", {}).get(
        "notes", ""
    )
    if partial:
        task_context += f"\nPartial result: {partial}"

    # Получаем порог из конфига
    supervisor_cfg = config.get("supervisor", {})
    threshold = int(
        supervisor_cfg.get("confidence_threshold", DEFAULT_SUPERVISOR_THRESHOLD)
    )

    reasoning = await supervisor_reasoning(question, task_context, db_path)

    if reasoning["confidence"] >= threshold and reasoning["answer"]:
        logger.info(
            "handle_worker_blocked: auto-resolve task_id=%s confidence=%d",
            task_id,
            reasoning["confidence"],
        )
        return f"directive:{reasoning['answer']}"

    # Не уверен — эскалация
    esc_id = create_escalation(
        task_id=task_id,
        escalation_type="worker_blocked",
        question=question,
        context=task_context[:500],
        db_path=db_path,
    )

    await tg_handler.notify_owner(
        f"task#{task_id[:8]}: воркер заблокирован.\n"
        f"Вопрос: {question[:200]}\n"
        f"Ответь текстом или /approve {task_id[:8]} / /reject {task_id[:8]}"
    )

    logger.info(
        "handle_worker_blocked: escalated task_id=%s escalation_id=%s",
        task_id,
        esc_id,
    )
    return "escalated"


async def handle_pending_approval(
    task: dict,
    tg_handler,
    db_path: Optional[str] = None,
) -> None:
    """
    Отправить уведомление владельцу для одобрения/отклонения задачи.

    Args:
        task: dict задачи из DB
        tg_handler: TelegramHandler для уведомлений
        db_path: путь к БД (для совместимости)
    """
    task_id = task["id"]
    description = task.get("description", "")
    short_desc = description[:200] if len(description) > 200 else description

    await tg_handler.notify_owner(
        f"task#{task_id[:8]}: требуется одобрение.\n"
        f"{short_desc}\n"
        f"/approve {task_id[:8]} или /reject {task_id[:8]}"
    )

    logger.info("handle_pending_approval: notified task_id=%s", task_id)


def create_escalation(
    task_id: str,
    escalation_type: str,
    question: str,
    context: str = "",
    db_path: Optional[str] = None,
    uuid_fn=None,
) -> str:
    """
    Создать запись эскалации в таблице escalations.

    Args:
        task_id: ID задачи
        escalation_type: тип эскалации (worker_blocked, low_confidence, ...)
        question: вопрос для владельца
        context: дополнительный контекст
        db_path: путь к БД
        uuid_fn: функция генерации UUID (DI для тестов)

    Returns:
        escalation_id (str)
    """
    gen_id = uuid_fn or (lambda: str(uuid.uuid4()))
    esc_id = gen_id()

    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO escalations
               (id, task_id, reason, question, context)
               VALUES (?, ?, ?, ?, ?)""",
            (esc_id, task_id, escalation_type, question, context),
        )

    logger.info(
        "create_escalation: id=%s task_id=%s type=%s",
        esc_id,
        task_id,
        escalation_type,
    )
    return esc_id


def resolve_escalation(
    escalation_id: str,
    resolution: str,
    db_path: Optional[str] = None,
) -> None:
    """
    Закрыть эскалацию с ответом.

    Args:
        escalation_id: ID эскалации
        resolution: ответ/резолюция
        db_path: путь к БД
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE escalations
               SET resolved=1, response=?, resolved_at=datetime('now')
               WHERE id=?""",
            (resolution, escalation_id),
        )

    logger.info("resolve_escalation: id=%s", escalation_id)


def get_open_escalation(
    task_id: str,
    db_path: Optional[str] = None,
) -> Optional[dict]:
    """
    Получить открытую эскалацию для задачи.

    Args:
        task_id: ID задачи
        db_path: путь к БД

    Returns:
        dict эскалации или None если нет открытых
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM escalations WHERE task_id=? AND resolved=0 "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    return dict(row) if row else None


async def route_owner_reply(
    text: str,
    tg_handler,
    db_path: Optional[str] = None,
) -> Optional[str]:
    """
    Найти открытую эскалацию и применить ответ владельца.

    Ищет последнюю открытую эскалацию (любого task_id), резолвит её.

    Args:
        text: текст ответа владельца
        tg_handler: TelegramHandler (для совместимости)
        db_path: путь к БД

    Returns:
        task_id если эскалация найдена и закрыта, None если нет открытых
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM escalations WHERE resolved=0 "
            "ORDER BY created_at DESC LIMIT 1",
        ).fetchone()

    if not row:
        logger.debug("route_owner_reply: no open escalations")
        return None

    esc = dict(row)
    esc_id = esc["id"]
    task_id = esc["task_id"]

    resolve_escalation(esc_id, text, db_path)

    logger.info(
        "route_owner_reply: resolved escalation=%s task_id=%s",
        esc_id,
        task_id,
    )
    return task_id
