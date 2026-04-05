"""
supervisor/planner.py — Planning engine for complex task decomposition.

Super-agent "менеджер": классифицирует сложность задачи, создаёт
структурированный план через Claude CLI, самопроверяет план через
supervisor_reasoning, форматирует для TG.

Инварианты:
- Нет shell=True
- Логи через logger, секреты через redact()
- DI: runner как параметр (для тестов)
- НЕ пишет в DB напрямую (caller управляет state transitions)
- НЕ запускает subprocess напрямую (только через runner/run_claude)
"""

import logging
from typing import Callable, Optional

from supervisor.json_guard import extract_json

logger = logging.getLogger(__name__)

# Слова-действия для проверки на расплывчатость описания (рус + англ)
_ACTION_WORDS = [
    # Русские
    "сделай",
    "добавь",
    "исправь",
    "удали",
    "создай",
    "реализуй",
    "настрой",
    "обнови",
    "замени",
    "перепиши",
    "напиши",
    "проверь",
    "проанализируй",
    "найди",
    "оптимизируй",
    "мигрируй",
    "протестируй",
    "убери",
    "перенеси",
    "измени",
    "покажи",
    "объясни",
    "запусти",
    # English
    "add",
    "fix",
    "create",
    "implement",
    "update",
    "remove",
    "write",
    "refactor",
    "deploy",
    "configure",
    "build",
    "test",
    "analyze",
    "find",
    "optimize",
    "migrate",
    "move",
    "change",
    "show",
    "run",
    "delete",
    "setup",
    "check",
    "review",
    "debug",
    "install",
]

# Ключевые слова, повышающие вероятность "complex"
_DEFAULT_COMPLEXITY_KEYWORDS = [
    "refactor",
    "migration",
    "redesign",
    "architecture",
    "security",
    "authentication",
    "database",
    "schema",
    "multi-file",
    "integration",
    "deploy",
    "infrastructure",
    "рефакторинг",
    "миграция",
    "архитектура",
    "безопасность",
    "аутентификация",
    "деплой",
    "инфраструктура",
]


async def classify_complexity(
    description: str,
    config: dict,
    runner: Optional[Callable] = None,
) -> str:
    """
    Классифицировать задачу как "simple" или "complex".

    Стратегия: гибрид (длина + ключевые слова + опциональный LLM).
    1. Длина описания > threshold → "complex"
    2. Количество ключевых слов >= 2 → "complex"
    3. Пограничные случаи → LLM-классификация (если runner доступен)

    Args:
        description: текст описания задачи
        config: секция supervisor.planning из конфига
        runner: DI для run_claude (тесты)

    Returns:
        "simple" или "complex"
    """
    threshold = int(config.get("complexity_threshold", 200))
    keywords = config.get("complexity_keywords", _DEFAULT_COMPLEXITY_KEYWORDS)

    desc_lower = description.lower()
    keyword_hits = sum(1 for kw in keywords if kw.lower() in desc_lower)

    # Правило 1: очень длинное + хотя бы 1 keyword → complex
    if len(description) > threshold * 3 and keyword_hits >= 1:
        logger.info(
            "classify_complexity: complex (length=%d > %d + keywords=%d)",
            len(description),
            threshold * 3,
            keyword_hits,
        )
        return "complex"

    # Правило 2: 3+ ключевых слов → complex (was 2)
    if keyword_hits >= 3:
        logger.info("classify_complexity: complex (keyword_hits=%d >= 3)", keyword_hits)
        return "complex"

    # Правило 3: короткое описание → simple
    if len(description) <= threshold:
        logger.info(
            "classify_complexity: simple (length=%d <= %d)", len(description), threshold
        )
        return "simple"

    # Правило 4: средняя длина + 2 keywords → complex
    if keyword_hits >= 2:
        logger.info(
            "classify_complexity: complex (medium length + keyword_hits=%d)",
            keyword_hits,
        )
        return "complex"

    # Пограничный случай: спросить LLM
    if runner:
        try:
            result = await _llm_classify(description, runner)
            logger.info("classify_complexity: %s (llm)", result)
            return result
        except Exception as exc:
            logger.warning(
                "classify_complexity: llm failed: %s, defaulting to simple", exc
            )
            return "simple"

    # Нет runner → по умолчанию simple (Sonnet справится с большинством задач)
    logger.info("classify_complexity: simple (borderline, no runner)")
    return "simple"


async def _llm_classify(description: str, runner: Callable) -> str:
    """Спросить Claude: simple или complex."""
    prompt = (
        "Классифицируй задачу как simple или complex.\n"
        "simple = одна функция, один файл, минимальные изменения.\n"
        "complex = несколько файлов, архитектурные решения, риски.\n\n"
        f"Задача: {description[:500]}\n\n"
        "Ответь ОДНИМ словом: simple или complex"
    )
    raw = await runner(prompt, cwd=None, timeout=90)
    raw_lower = raw.strip().lower()
    if "complex" in raw_lower:
        return "complex"
    return "simple"


async def create_plan(
    task_description: str,
    task_id: str,
    config: dict,
    feedback: str = "",
    revision: int = 0,
    db_path: Optional[str] = None,
    runner: Optional[Callable] = None,
) -> dict:
    """
    Создать структурированный план для сложной задачи.

    Использует Claude CLI как super-agent. Самопроверяет план
    через supervisor_reasoning.

    Args:
        task_description: полное описание задачи
        task_id: UUID задачи
        config: полный конфиг (для supervisor_reasoning)
        feedback: фидбек от владельца при ревизии (пусто при первом проходе)
        revision: номер текущей ревизии (0-based)
        db_path: путь к БД
        runner: DI для run_claude

    Returns:
        dict с ключами: title, context, approach, tasks, risks,
        estimate, self_review, confidence

    Raises:
        PlanningError (через caller): если план не удалось создать
    """
    from supervisor.claude_runner import run_claude

    actual_runner = runner or run_claude

    prompt = _build_planning_prompt(task_description, task_id, feedback, revision)

    raw = await actual_runner(prompt, cwd=None, timeout=300)

    parsed = extract_json(raw)
    if not parsed:
        logger.warning("create_plan: no valid JSON in response task_id=%s", task_id)
        return {}

    valid, err = validate_plan_schema(parsed)
    if not valid:
        logger.warning("create_plan: invalid plan schema task_id=%s: %s", task_id, err)
        return {}

    # Самопроверка плана через supervisor_reasoning
    plan = await _self_review_plan(parsed, task_description, db_path)

    logger.info(
        "create_plan: plan created task_id=%s revision=%d confidence=%d tasks=%d",
        task_id,
        revision,
        plan.get("confidence", 0),
        len(plan.get("tasks", [])),
    )
    return plan


def _build_planning_prompt(
    task_description: str,
    task_id: str,
    feedback: str = "",
    revision: int = 0,
) -> str:
    """Построить промпт для planning super-agent."""
    feedback_section = ""
    if feedback and revision > 0:
        feedback_section = (
            f"\n--- ФИДБЕК ВЛАДЕЛЬЦА (ревизия {revision}) ---\n"
            f"{feedback}\n"
            f"--- КОНЕЦ ФИДБЕКА ---\n\n"
            f"Учти фидбек и доработай план.\n\n"
        )

    return f"""\
Ты — менеджер проекта. Создай структурированный план реализации задачи.

Задача (task_id: {task_id[:8]}):
{task_description}

{feedback_section}Проанализируй задачу и создай детальный план. Для каждого шага определи:
- Какие файлы нужно менять
- Что конкретно делать
- Как проверить результат

ОБЯЗАТЕЛЬНО ответь JSON в формате:
<<<JSON>>>
{{
    "title": "краткое название плана",
    "context": "что нужно сделать и зачем",
    "approach": "высокоуровневая стратегия реализации",
    "tasks": [
        {{
            "step": 1,
            "description": "описание шага",
            "files_to_modify": ["path/to/file.py"],
            "changes": "что конкретно менять",
            "verification": "как проверить"
        }}
    ],
    "risks": "потенциальные риски и как их минимизировать",
    "estimate": "количество шагов, оценка сложности"
}}
<<<END>>>
"""


async def _self_review_plan(
    plan: dict,
    task_description: str,
    db_path: Optional[str] = None,
) -> dict:
    """
    Super-agent самопроверяет план через supervisor_reasoning.

    Задаёт вопросы о полноте, рисках, осуществимости.
    Добавляет self_review и confidence в план.

    Returns:
        Обновлённый plan dict с ключами "self_review" и "confidence".
    """
    from supervisor.escalation import supervisor_reasoning

    plan_summary = (
        f"План: {plan.get('title', '?')}\n"
        f"Подход: {plan.get('approach', '?')}\n"
        f"Шагов: {len(plan.get('tasks', []))}\n"
        f"Риски: {plan.get('risks', '?')}"
    )

    question = (
        "Оцени качество этого плана реализации:\n"
        f"{plan_summary}\n\n"
        "Полный ли он? Есть ли пропущенные шаги? Реалистичны ли оценки?"
    )

    try:
        result = await supervisor_reasoning(question, task_description, db_path)
        plan["self_review"] = result.get("answer", "")
        plan["confidence"] = result.get("confidence", 50)
    except Exception as exc:
        logger.warning("_self_review_plan: supervisor_reasoning failed: %s", exc)
        plan["self_review"] = ""
        plan["confidence"] = 50

    return plan


def validate_plan_schema(obj: dict) -> tuple[bool, str]:
    """
    Валидировать структуру плана.

    Returns:
        (True, "") если валиден, (False, "описание ошибки") если нет.
    """
    if not isinstance(obj, dict):
        return False, "plan is not a dict"

    required = ("title", "context", "approach", "tasks")
    for key in required:
        if key not in obj:
            return False, f"missing required key: {key}"

    if not isinstance(obj["title"], str) or not obj["title"].strip():
        return False, "title must be a non-empty string"

    if not isinstance(obj["tasks"], list) or len(obj["tasks"]) == 0:
        return False, "tasks must be a non-empty list"

    for i, task in enumerate(obj["tasks"]):
        if not isinstance(task, dict):
            return False, f"tasks[{i}] is not a dict"
        if "description" not in task:
            return False, f"tasks[{i}] missing description"

    return True, ""


def format_plan_for_tg(plan: dict, task_id: str) -> str:
    """
    Форматировать план для отправки в Telegram.

    Учитывает лимит 4096 символов. При превышении — обрезает.

    Args:
        plan: структурированный план
        task_id: UUID задачи (для short ID)

    Returns:
        Форматированный текст.
    """
    lines = []
    short_id = task_id[:8]

    lines.append(f"# Plan #{short_id}: {plan.get('title', 'Untitled')}")
    lines.append("")

    # Контекст
    ctx = plan.get("context", "")
    if ctx:
        lines.append(f"Контекст: {ctx}")
        lines.append("")

    # Подход
    approach = plan.get("approach", "")
    if approach:
        lines.append(f"Подход: {approach}")
        lines.append("")

    # Задачи
    tasks = plan.get("tasks", [])
    if tasks:
        lines.append("Задачи:")
        for t in tasks:
            step = t.get("step", "?")
            desc = t.get("description", "?")
            files = t.get("files_to_modify", [])
            files_str = ", ".join(files) if files else "-"
            lines.append(f"  {step}. {desc}")
            lines.append(f"     Файлы: {files_str}")
        lines.append("")

    # Риски
    risks = plan.get("risks", "")
    if risks:
        lines.append(f"Риски: {risks}")
        lines.append("")

    # Оценка
    estimate = plan.get("estimate", "")
    if estimate:
        lines.append(f"Оценка: {estimate}")
        lines.append("")

    # Self-review
    self_review = plan.get("self_review", "")
    confidence = plan.get("confidence", 0)
    if self_review:
        lines.append(f"Самооценка (confidence={confidence}): {self_review[:200]}")
        lines.append("")

    # Кнопки действий
    lines.append(f"/approve {short_id} -- одобрить план")
    lines.append(f"/revise {short_id} <комментарий> -- доработать")
    lines.append(f"/reject {short_id} -- отклонить")

    text = "\n".join(lines)

    # TG лимит: 4096 символов
    if len(text) > 4000:
        text = text[:3950] + f"\n...(обрезано)\n/plan {short_id} -- полный план"

    return text


def needs_clarification(description: str, config: dict) -> bool:
    """
    Check if task description is too vague and needs clarification.

    Criteria:
    - Very short description (< min_length chars)
    - No verbs/action words (Russian and English)

    Config keys: clarification_min_length (default 30)

    Args:
        description: текст описания задачи
        config: секция supervisor.planning из конфига

    Returns:
        True если описание слишком расплывчатое.
    """
    min_length = int(config.get("clarification_min_length", 30))
    desc_stripped = description.strip()

    # Check for action indicators (Russian and English)
    desc_lower = desc_stripped.lower()
    has_action = any(word in desc_lower for word in _ACTION_WORDS)

    # Trigger clarification only if BOTH: short AND no action word
    # (avoid false positives on longer descriptions that just lack a verb)
    if len(desc_stripped) < min_length and not has_action:
        return True

    # Very short with no action — definitely vague
    if len(desc_stripped) < 15:
        return True

    return False


async def generate_clarifying_questions(
    description: str,
    runner: Optional[Callable] = None,
) -> str:
    """
    Generate 3-5 clarifying questions for a vague task description.

    Uses Claude CLI to produce questions tailored to the specific description.
    Falls back to a generic set on runner failure.

    Args:
        description: текст расплывчатого описания
        runner: DI для run_claude (для тестов)

    Returns:
        Formatted text with numbered questions for TG.
    """
    from supervisor.claude_runner import run_claude

    actual_runner = runner or run_claude

    prompt = (
        "Ты — менеджер проекта. Тебе пришла задача, но описание слишком расплывчатое.\n\n"
        f"Задача: {description}\n\n"
        "Задай 3-5 уточняющих вопросов чтобы понять:\n"
        "1. Что конкретно нужно сделать\n"
        "2. Какие ограничения/требования\n"
        "3. Какой ожидаемый результат\n\n"
        "Формат: пронумерованный список вопросов, без лишнего текста."
    )

    try:
        raw = await actual_runner(prompt, cwd=None, timeout=60)
        return raw.strip()
    except Exception as exc:
        logger.warning("generate_clarifying_questions: runner failed: %s", exc)
        return (
            "Уточни задачу:\n"
            "1. Что конкретно нужно сделать?\n"
            "2. Какие файлы/модули затронуты?\n"
            "3. Какой ожидаемый результат?"
        )
