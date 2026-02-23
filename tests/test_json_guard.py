"""
tests/test_json_guard.py — тесты для supervisor/json_guard.py

Запуск: pytest tests/test_json_guard.py -v
"""

import pytest
from supervisor.json_guard import (
    extract_json,
    validate_worker_schema,
    validate_reviewer_schema,
    validate_health_schema,
)


# ── extract_json ─────────────────────────────────────────────────────────────

WORKER_JSON = {
    "status": "done",
    "confidence": 85,
    "result": {
        "repos": [{"alias": "api", "changed_files": ["a.py"], "entrypoint": None}],
        "notes": "сделал то-то",
    },
    "question": None,
}

WORKER_JSON_STR = (
    '{"status": "done", "confidence": 85, '
    '"result": {"repos": [{"alias": "api", "changed_files": ["a.py"], "entrypoint": null}], '
    '"notes": "done"}, "question": null}'
)


def test_extract_json_with_markers():
    """Счастливый путь: JSON между маркерами."""
    raw = f"Текст до\n<<<JSON>>>\n{WORKER_JSON_STR}\n<<<END>>>\nТекст после"
    result = extract_json(raw)
    assert result is not None
    assert result["status"] == "done"
    assert result["confidence"] == 85


def test_extract_json_fallback_no_markers():
    """Fallback: JSON без маркеров — первый объект в тексте."""
    raw = f"Некоторый текст {WORKER_JSON_STR} ещё текст"
    result = extract_json(raw)
    assert result is not None
    assert result["status"] == "done"


def test_extract_json_invalid_json_in_markers():
    """Невалидный JSON в маркерах — пробуем fallback."""
    raw = "<<<JSON>>>\nnot valid json\n<<<END>>>"
    result = extract_json(raw)
    assert result is None


def test_extract_json_empty_string():
    """Пустая строка → None."""
    assert extract_json("") is None


def test_extract_json_no_json_at_all():
    """Строка без JSON → None."""
    assert extract_json("просто текст без JSON") is None


def test_extract_json_prefers_markers_over_fallback():
    """Если есть маркеры, использует их, а не fallback."""
    inner = '{"source": "markers"}'
    outer = '{"source": "fallback"}'
    raw = f"{outer} <<<JSON>>>{inner}<<<END>>>"
    result = extract_json(raw)
    assert result["source"] == "markers"


def test_extract_json_array_returns_none():
    """JSON массив (не объект) → None."""
    raw = "<<<JSON>>>[1, 2, 3]<<<END>>>"
    result = extract_json(raw)
    assert result is None


# ── validate_worker_schema ───────────────────────────────────────────────────

def test_worker_schema_happy_path():
    ok, msg = validate_worker_schema(WORKER_JSON)
    assert ok is True
    assert msg == ""


def test_worker_schema_blocked_status():
    obj = {"status": "blocked", "confidence": 50, "result": None, "question": "Как быть?"}
    ok, msg = validate_worker_schema(obj)
    assert ok is True


def test_worker_schema_invalid_status():
    obj = {**WORKER_JSON, "status": "in_progress"}
    ok, msg = validate_worker_schema(obj)
    assert ok is False
    assert "status" in msg


def test_worker_schema_confidence_out_of_range():
    obj = {**WORKER_JSON, "confidence": 150}
    ok, msg = validate_worker_schema(obj)
    assert ok is False
    assert "confidence" in msg


def test_worker_schema_confidence_zero_valid():
    obj = {**WORKER_JSON, "confidence": 0}
    ok, msg = validate_worker_schema(obj)
    assert ok is True


def test_worker_schema_missing_repos_when_done():
    obj = {
        "status": "done",
        "confidence": 90,
        "result": {"notes": "done"},
        "question": None,
    }
    ok, msg = validate_worker_schema(obj)
    assert ok is False
    assert "repos" in msg


def test_worker_schema_question_must_be_string_or_null():
    obj = {**WORKER_JSON, "question": 42}
    ok, msg = validate_worker_schema(obj)
    assert ok is False
    assert "question" in msg


def test_worker_schema_question_string_valid():
    obj = {**WORKER_JSON, "question": "Что делать?"}
    ok, msg = validate_worker_schema(obj)
    assert ok is True


# ── validate_reviewer_schema ─────────────────────────────────────────────────

REVIEWER_JSON = {
    "verdict": "APPROVED",
    "feedback": "Код хорош",
    "issues": [],
}

REVIEWER_WITH_ISSUES = {
    "verdict": "NEEDS_CHANGES",
    "feedback": "Есть проблемы",
    "issues": [
        {"repo": "api", "file": "a.py", "line": 42, "type": "bug", "message": "ошибка"},
    ],
}


def test_reviewer_schema_approved_happy_path():
    ok, msg = validate_reviewer_schema(REVIEWER_JSON)
    assert ok is True


def test_reviewer_schema_needs_changes_with_issues():
    ok, msg = validate_reviewer_schema(REVIEWER_WITH_ISSUES)
    assert ok is True


def test_reviewer_schema_invalid_verdict():
    obj = {**REVIEWER_JSON, "verdict": "REJECTED"}
    ok, msg = validate_reviewer_schema(obj)
    assert ok is False
    assert "verdict" in msg


def test_reviewer_schema_missing_feedback():
    obj = {"verdict": "APPROVED", "issues": []}
    ok, msg = validate_reviewer_schema(obj)
    assert ok is False
    assert "feedback" in msg


def test_reviewer_schema_issues_not_list():
    obj = {**REVIEWER_JSON, "issues": "none"}
    ok, msg = validate_reviewer_schema(obj)
    assert ok is False
    assert "issues" in msg


def test_reviewer_schema_issue_missing_required_field():
    obj = {
        "verdict": "NEEDS_CHANGES",
        "feedback": "bad",
        "issues": [{"repo": "api", "file": "a.py"}],  # missing type, message
    }
    ok, msg = validate_reviewer_schema(obj)
    assert ok is False


# ── validate_health_schema ───────────────────────────────────────────────────

HEALTH_JSON = {
    "status": "done",
    "confidence": 80,
    "health": {
        "overall": "GREEN",
        "findings": [],
        "auto_actions": [],
        "create_tasks": [],
        "tg_alert": None,
        "notion_detail": "подробности",
    },
    "question": None,
}

HEALTH_WITH_FINDINGS = {
    "status": "done",
    "confidence": 70,
    "health": {
        "overall": "YELLOW",
        "findings": [
            {
                "severity": "med",
                "title": "Диск 80%",
                "evidence": "df -h показал 80%",
                "suggestion": "Почистить логи",
            }
        ],
        "auto_actions": ["cleanup_worktrees"],
        "create_tasks": [
            {"title": "Оптимизация", "summary": "надо", "priority": "P2"},
        ],
        "tg_alert": "YELLOW: диск 80%",
        "notion_detail": "...",
    },
    "question": None,
}


def test_health_schema_green_happy_path():
    ok, msg = validate_health_schema(HEALTH_JSON)
    assert ok is True


def test_health_schema_yellow_with_findings():
    ok, msg = validate_health_schema(HEALTH_WITH_FINDINGS)
    assert ok is True


def test_health_schema_invalid_overall():
    import copy
    obj = copy.deepcopy(HEALTH_JSON)
    obj["health"]["overall"] = "ORANGE"
    ok, msg = validate_health_schema(obj)
    assert ok is False
    assert "overall" in msg


def test_health_schema_invalid_finding_severity():
    import copy
    obj = copy.deepcopy(HEALTH_WITH_FINDINGS)
    obj["health"]["findings"][0]["severity"] = "critical"
    ok, msg = validate_health_schema(obj)
    assert ok is False
    assert "severity" in msg


def test_health_schema_missing_health_key():
    obj = {**HEALTH_JSON}
    del obj["health"]
    ok, msg = validate_health_schema(obj)
    assert ok is False
    assert "health" in msg


def test_health_schema_create_tasks_missing_field():
    import copy
    obj = copy.deepcopy(HEALTH_JSON)
    obj["health"]["create_tasks"] = [{"title": "X"}]  # missing summary, priority
    ok, msg = validate_health_schema(obj)
    assert ok is False
