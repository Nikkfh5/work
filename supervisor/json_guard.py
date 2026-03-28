"""
supervisor/json_guard.py — парсинг и валидация JSON из stdout агентов.

Инварианты:
- НЕ пишет файлы
- НЕ пишет в DB
- НЕ запускает subprocess
- Только парсинг raw text и структурная валидация

Протоколы агентов:
- Worker:   {"status", "confidence", "result": {"repos", "notes"}, "question"}
- Reviewer: {"verdict", "feedback", "issues"}
- Health:   {"status", "confidence", "health": {"overall", "findings", ...}, "question"}
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Маркеры для извлечения JSON из вывода агента
JSON_START_MARKER = "<<<JSON>>>"
JSON_END_MARKER = "<<<END>>>"

# No single regex — use brace-counting extractor instead

# Допустимые значения полей
VALID_WORKER_STATUSES = {"done", "blocked", "error"}
VALID_REVIEWER_VERDICTS = {"APPROVED", "NEEDS_CHANGES"}
VALID_HEALTH_OVERALL = {"GREEN", "YELLOW", "RED"}
VALID_HEALTH_SEVERITIES = {"low", "med", "high"}


# ── Извлечение JSON ──────────────────────────────────────────────────────────


def extract_json(raw: str) -> Optional[dict]:
    """
    Извлечь JSON-объект из строки вывода агента.

    Двухступенчатый алгоритм:
    1. Ищет JSON между маркерами <<<JSON>>>...<<<END>>>
    2. Fallback: первый JSON-объект по regex

    Args:
        raw: raw stdout агента

    Returns:
        Распарсенный dict или None если JSON не найден/невалиден.
    """
    if not raw:
        return None

    # Шаг 1: поиск по маркерам
    start = raw.find(JSON_START_MARKER)
    end = raw.find(JSON_END_MARKER)

    if start != -1 and end != -1 and end > start:
        json_text = raw[start + len(JSON_START_MARKER) : end].strip()
        parsed = _try_parse(json_text, source="markers")
        if parsed is not None:
            return parsed
        logger.warning("extract_json: found markers but JSON invalid, trying fallback")

    # Шаг 2: fallback — extract ALL JSON candidates via brace counting,
    # then prefer the one that matches an agent schema
    candidates = _extract_json_candidates(raw)
    first_valid = None
    for i, candidate in enumerate(candidates):
        parsed = _try_parse(candidate, source=f"fallback[{i}]")
        if parsed is not None:
            if _looks_like_agent_json(parsed):
                return parsed
            if first_valid is None:
                first_valid = parsed

    if first_valid is not None:
        return first_valid

    logger.warning("extract_json: no valid JSON found in output (%d chars)", len(raw))
    return None


def _looks_like_agent_json(obj: dict) -> bool:
    """Check if parsed dict looks like a worker/reviewer/health response."""
    # Worker: has "status" field
    if "status" in obj and obj.get("status") in VALID_WORKER_STATUSES:
        return True
    # Reviewer: has "verdict" field
    if "verdict" in obj and obj.get("verdict") in VALID_REVIEWER_VERDICTS:
        return True
    # Health: has "health" dict with "overall"
    if isinstance(obj.get("health"), dict):
        return True
    return False


def _extract_json_candidates(raw: str) -> list[str]:
    """
    Extract all top-level JSON object candidates via brace counting.

    Handles arbitrary nesting depth (unlike regex).
    Returns list of candidate strings, ordered by position in raw.
    """
    candidates = []
    i = 0
    while i < len(raw):
        if raw[i] == "{":
            depth = 0
            start = i
            in_string = False
            escape_next = False
            for j in range(i, len(raw)):
                ch = raw[j]
                if escape_next:
                    escape_next = False
                    continue
                if ch == "\\":
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(raw[start : j + 1])
                        i = j + 1
                        break
            else:
                # Unbalanced braces — skip this {
                i += 1
        else:
            i += 1
    return candidates


def _try_parse(text: str, source: str) -> Optional[dict]:
    """Попытаться распарсить JSON. Возвращает dict или None."""
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            logger.warning("extract_json[%s]: JSON is not an object", source)
            return None
        return obj
    except json.JSONDecodeError as exc:
        logger.debug("extract_json[%s]: parse error: %s", source, exc)
        return None


# ── Валидация схем ───────────────────────────────────────────────────────────


def validate_worker_schema(obj: dict) -> tuple[bool, str]:
    """
    Валидировать JSON от Worker-агента.

    Схема:
        status: "done" | "blocked" | "error"
        confidence: int 0..100
        result: {repos: list, notes: str}
        question: str | null

    Returns:
        (True, "") если валидно, (False, "сообщение об ошибке") иначе.
    """
    if not isinstance(obj, dict):
        return False, "worker schema: root must be an object"

    status = obj.get("status")
    if status not in VALID_WORKER_STATUSES:
        return (
            False,
            f"worker schema: status={status!r} must be one of {VALID_WORKER_STATUSES}",
        )

    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 100):
        return (
            False,
            f"worker schema: confidence={confidence!r} must be a number 0..100",
        )

    result = obj.get("result")
    if status == "done":
        if not isinstance(result, dict):
            return False, "worker schema: result must be an object when status=done"
        repos = result.get("repos")
        if not isinstance(repos, list):
            return False, "worker schema: result.repos must be a list"
        for i, repo in enumerate(repos):
            if not isinstance(repo, dict):
                return False, f"worker schema: result.repos[{i}] must be an object"
            if "alias" not in repo:
                return False, f"worker schema: result.repos[{i}] missing 'alias'"
            if "changed_files" not in repo:
                return (
                    False,
                    f"worker schema: result.repos[{i}] missing 'changed_files'",
                )

    # question может быть null или str
    question = obj.get("question")
    if question is not None and not isinstance(question, str):
        return (
            False,
            f"worker schema: question must be string or null, got {type(question).__name__}",
        )

    return True, ""


def validate_reviewer_schema(obj: dict) -> tuple[bool, str]:
    """
    Валидировать JSON от Reviewer-агента.

    Схема:
        verdict:  "APPROVED" | "NEEDS_CHANGES"
        feedback: str
        issues:   list of {repo, file, line, type, message}

    Returns:
        (True, "") если валидно, (False, "сообщение об ошибке") иначе.
    """
    if not isinstance(obj, dict):
        return False, "reviewer schema: root must be an object"

    verdict = obj.get("verdict")
    if verdict not in VALID_REVIEWER_VERDICTS:
        return (
            False,
            f"reviewer schema: verdict={verdict!r} must be one of {VALID_REVIEWER_VERDICTS}",
        )

    feedback = obj.get("feedback")
    if not isinstance(feedback, str):
        return (
            False,
            f"reviewer schema: feedback must be a string, got {type(feedback).__name__}",
        )

    issues = obj.get("issues")
    if not isinstance(issues, list):
        return False, "reviewer schema: issues must be a list"

    for i, issue in enumerate(issues):
        if not isinstance(issue, dict):
            return False, f"reviewer schema: issues[{i}] must be an object"
        for required_field in ("repo", "file", "type", "message"):
            if required_field not in issue:
                return False, f"reviewer schema: issues[{i}] missing '{required_field}'"

    return True, ""


def validate_health_schema(obj: dict) -> tuple[bool, str]:
    """
    Валидировать JSON от Health Monitor агента.

    Схема:
        status:     "done" | "blocked" | "error"
        confidence: int 0..100
        health:
            overall:      "GREEN" | "YELLOW" | "RED"
            findings:     list of {severity, title, evidence, suggestion}
            auto_actions: list of str
            create_tasks: list of {title, summary, priority}
        question: str | null

    Returns:
        (True, "") если валидно, (False, "сообщение об ошибке") иначе.
    """
    if not isinstance(obj, dict):
        return False, "health schema: root must be an object"

    status = obj.get("status")
    if status not in VALID_WORKER_STATUSES:
        return (
            False,
            f"health schema: status={status!r} must be one of {VALID_WORKER_STATUSES}",
        )

    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 100):
        return (
            False,
            f"health schema: confidence={confidence!r} must be a number 0..100",
        )

    health = obj.get("health")
    if not isinstance(health, dict):
        return False, "health schema: 'health' must be an object"

    overall = health.get("overall")
    if overall not in VALID_HEALTH_OVERALL:
        return (
            False,
            f"health schema: health.overall={overall!r} must be one of {VALID_HEALTH_OVERALL}",
        )

    findings = health.get("findings")
    if not isinstance(findings, list):
        return False, "health schema: health.findings must be a list"

    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            return False, f"health schema: findings[{i}] must be an object"
        severity = finding.get("severity")
        if severity not in VALID_HEALTH_SEVERITIES:
            return (
                False,
                f"health schema: findings[{i}].severity={severity!r} must be one of {VALID_HEALTH_SEVERITIES}",
            )
        for field in ("title", "evidence", "suggestion"):
            if field not in finding:
                return False, f"health schema: findings[{i}] missing '{field}'"

    auto_actions = health.get("auto_actions")
    if not isinstance(auto_actions, list):
        return False, "health schema: health.auto_actions must be a list"

    create_tasks = health.get("create_tasks")
    if not isinstance(create_tasks, list):
        return False, "health schema: health.create_tasks must be a list"

    for i, task in enumerate(create_tasks):
        if not isinstance(task, dict):
            return False, f"health schema: create_tasks[{i}] must be an object"
        for field in ("title", "summary", "priority"):
            if field not in task:
                return False, f"health schema: create_tasks[{i}] missing '{field}'"

    question = obj.get("question")
    if question is not None and not isinstance(question, str):
        return False, "health schema: question must be string or null"

    return True, ""
