"""
tests/test_planner.py — тесты для supervisor/planner.py.

12 тестов:
  1. classify_complexity: short description -> "simple"
  2. classify_complexity: long description -> "complex"
  3. classify_complexity: 2+ keywords -> "complex"
  4. classify_complexity: custom threshold from config
  5. classify_complexity: borderline + runner -> LLM result
  6. classify_complexity: borderline, no runner -> "complex"
  7. create_plan: valid plan JSON -> valid dict
  8. create_plan: feedback + revision > 0 -> prompt includes feedback
  9. create_plan: runner error -> empty dict
  10. validate_plan_schema: valid plan -> (True, "")
  11. validate_plan_schema: missing title -> (False, "missing...")
  12. format_plan_for_tg: result <= 4096 chars
"""

import pytest
from unittest.mock import AsyncMock, patch

from supervisor.planner import (
    classify_complexity,
    create_plan,
    generate_clarifying_questions,
    needs_clarification,
    validate_plan_schema,
    format_plan_for_tg,
)


# ── Valid plan fixtures ─────────────────────────────────────────────────────────

VALID_PLAN_JSON_RESPONSE = """\
<<<JSON>>>
{"title": "Test Plan", "context": "test", "approach": "test", "tasks": [{"step": 1, "description": "do X", "files_to_modify": ["a.py"], "changes": "change Y", "verification": "run tests"}], "risks": "none", "estimate": "1 step"}
<<<END>>>
"""

VALID_PLAN_DICT = {
    "title": "Test Plan",
    "context": "test",
    "approach": "test",
    "tasks": [
        {
            "step": 1,
            "description": "do X",
            "files_to_modify": ["a.py"],
            "changes": "change Y",
            "verification": "run tests",
        }
    ],
    "risks": "none",
    "estimate": "1 step",
}

SELF_REVIEW_RESPONSE = {"answer": "looks good", "confidence": 80}


# ── 1. classify_complexity: short description -> "simple" ───────────────────────


@pytest.mark.asyncio
async def test_classify_simple_short():
    """Short description (< threshold) with no keywords -> 'simple'."""
    config = {"complexity_threshold": 200, "complexity_keywords": ["refactor", "migration"]}
    result = await classify_complexity("Fix a typo in readme", config)
    assert result == "simple"


# ── 2. classify_complexity: long description -> "complex" ───────────────────────


@pytest.mark.asyncio
async def test_classify_complex_long():
    """Description longer than threshold*2 -> 'complex'."""
    config = {"complexity_threshold": 50, "complexity_keywords": []}
    long_desc = "a" * 101  # > 50*2 = 100
    result = await classify_complexity(long_desc, config)
    assert result == "complex"


# ── 3. classify_complexity: 2+ keywords -> "complex" ───────────────────────────


@pytest.mark.asyncio
async def test_classify_complex_keywords():
    """Two or more keyword hits -> 'complex'."""
    config = {
        "complexity_threshold": 1000,
        "complexity_keywords": ["refactor", "migration", "security"],
    }
    desc = "Need to refactor the auth module and run a migration"
    result = await classify_complexity(desc, config)
    assert result == "complex"


# ── 4. classify_complexity: custom threshold from config ────────────────────────


@pytest.mark.asyncio
async def test_classify_respects_config_threshold():
    """Custom threshold=10: short desc (9 chars) with no keywords -> 'simple'."""
    config = {"complexity_threshold": 10, "complexity_keywords": []}
    result = await classify_complexity("small fix", config)
    assert result == "simple"


# ── 5. classify_complexity: borderline + runner -> LLM result ───────────────────


@pytest.mark.asyncio
async def test_classify_borderline_uses_llm():
    """Borderline case (len > threshold but < threshold*2, 1 keyword) with runner -> LLM."""
    config = {
        "complexity_threshold": 20,
        "complexity_keywords": ["refactor"],
    }
    # len=30: > threshold(20) but < threshold*2(40), 1 keyword hit -> borderline
    desc = "refactor this small function"  # 28 chars, 1 keyword
    runner = AsyncMock(return_value="simple")
    result = await classify_complexity(desc, config, runner=runner)
    assert result == "simple"
    runner.assert_called_once()


# ── 6. classify_complexity: borderline, no runner -> "complex" ──────────────────


@pytest.mark.asyncio
async def test_classify_borderline_no_runner_defaults_complex():
    """Borderline case without runner -> defaults to 'complex'."""
    config = {
        "complexity_threshold": 20,
        "complexity_keywords": ["refactor"],
    }
    desc = "refactor this small function"  # borderline: 1 keyword, len > threshold
    result = await classify_complexity(desc, config, runner=None)
    assert result == "complex"


# ── 7. create_plan: valid plan JSON -> valid dict ───────────────────────────────


@pytest.mark.asyncio
async def test_create_plan_valid(db_path):
    """Mock runner returns valid plan JSON -> returns valid plan dict with self_review."""
    runner = AsyncMock(return_value=VALID_PLAN_JSON_RESPONSE)
    config = {}

    with patch(
        "supervisor.escalation.supervisor_reasoning",
        new_callable=AsyncMock,
        return_value=SELF_REVIEW_RESPONSE,
    ):
        result = await create_plan(
            task_description="Implement feature X",
            task_id="aaaa1111-bbbb-cccc-dddd-eeeeeeee0001",
            config=config,
            db_path=db_path,
            runner=runner,
        )

    assert result["title"] == "Test Plan"
    assert result["context"] == "test"
    assert result["approach"] == "test"
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["description"] == "do X"
    assert result["self_review"] == "looks good"
    assert result["confidence"] == 80
    runner.assert_called_once()


# ── 8. create_plan: feedback + revision > 0 -> prompt includes feedback ─────────


@pytest.mark.asyncio
async def test_create_plan_with_feedback_includes_revision(db_path):
    """Feedback with revision > 0 -> planning prompt includes feedback section."""
    captured_prompt = {}

    async def capturing_runner(prompt, cwd=None, timeout=300):
        captured_prompt["text"] = prompt
        return VALID_PLAN_JSON_RESPONSE

    with patch(
        "supervisor.escalation.supervisor_reasoning",
        new_callable=AsyncMock,
        return_value=SELF_REVIEW_RESPONSE,
    ):
        result = await create_plan(
            task_description="Build module Y",
            task_id="bbbb2222-cccc-dddd-eeee-ffffffffffff",
            config={},
            feedback="Add more error handling",
            revision=2,
            db_path=db_path,
            runner=capturing_runner,
        )

    assert result != {}
    prompt_text = captured_prompt["text"]
    assert "ФИДБЕК ВЛАДЕЛЬЦА" in prompt_text
    assert "ревизия 2" in prompt_text
    assert "Add more error handling" in prompt_text


# ── 9. create_plan: runner error -> empty dict ──────────────────────────────────


@pytest.mark.asyncio
async def test_create_plan_runner_error_returns_empty(db_path):
    """Runner returns non-JSON garbage -> extract_json fails -> returns {}."""
    runner = AsyncMock(return_value="ERROR: something went wrong, no JSON here")

    result = await create_plan(
        task_description="Build module Z",
        task_id="cccc3333-dddd-eeee-ffff-000000000000",
        config={},
        db_path=db_path,
        runner=runner,
    )

    assert result == {}


# ── 10. validate_plan_schema: valid plan -> (True, "") ──────────────────────────


def test_validate_plan_schema_valid():
    """Valid plan dict -> (True, '')."""
    valid, err = validate_plan_schema(VALID_PLAN_DICT)
    assert valid is True
    assert err == ""


# ── 11. validate_plan_schema: missing fields -> (False, "missing...") ───────────


@pytest.mark.parametrize(
    "missing_key",
    ["title", "context", "approach", "tasks"],
)
def test_validate_plan_schema_missing_fields(missing_key):
    """Missing required key -> (False, 'missing required key: ...')."""
    plan = dict(VALID_PLAN_DICT)
    del plan[missing_key]
    valid, err = validate_plan_schema(plan)
    assert valid is False
    assert "missing" in err.lower() or missing_key in err


# ── 12. format_plan_for_tg: result <= 4096 chars ───────────────────────────────


def test_format_plan_tg_within_limit():
    """Formatted plan fits within TG 4096 char limit."""
    task_id = "dddd4444-eeee-ffff-0000-111111111111"
    plan = {
        "title": "Big Refactoring Plan",
        "context": "Refactor the entire codebase for better maintainability",
        "approach": "Incremental refactoring with tests at each step",
        "tasks": [
            {
                "step": i,
                "description": f"Step {i}: refactor module_{i}.py with comprehensive changes",
                "files_to_modify": [f"module_{i}.py"],
                "changes": f"Rewrite module {i}",
                "verification": f"Run tests for module {i}",
            }
            for i in range(1, 21)  # 20 tasks to push length
        ],
        "risks": "High complexity, potential regressions in multiple modules",
        "estimate": "20 steps, approximately 3 days of work",
        "self_review": "Plan looks comprehensive but ambitious. " * 10,
        "confidence": 75,
    }

    text = format_plan_for_tg(plan, task_id)

    assert len(text) <= 4096
    assert task_id[:8] in text
    assert "/approve" in text
    assert "/revise" in text or "/reject" in text


# ── Extra: validate_plan_schema edge cases ──────────────────────────────────────


def test_validate_plan_schema_empty_title():
    """Empty title string -> (False, 'title must be...')."""
    plan = dict(VALID_PLAN_DICT)
    plan["title"] = "   "
    valid, err = validate_plan_schema(plan)
    assert valid is False
    assert "title" in err


def test_validate_plan_schema_empty_tasks():
    """Empty tasks list -> (False, 'tasks must be...')."""
    plan = dict(VALID_PLAN_DICT)
    plan["tasks"] = []
    valid, err = validate_plan_schema(plan)
    assert valid is False
    assert "tasks" in err


def test_validate_plan_schema_task_missing_description():
    """Task without description -> (False, 'tasks[0] missing description')."""
    plan = dict(VALID_PLAN_DICT)
    plan["tasks"] = [{"step": 1, "files_to_modify": ["a.py"]}]
    valid, err = validate_plan_schema(plan)
    assert valid is False
    assert "description" in err


# ── needs_clarification tests ──────────────────────────────────────────────


def test_needs_clarification_short():
    """Short description (< 30 chars) -> needs clarification."""
    assert needs_clarification("авторизация", {}) is True


def test_needs_clarification_no_action():
    """Very short description (< 15 chars) without action words -> needs clarification."""
    assert needs_clarification("логирование", {}) is True


def test_needs_clarification_ok():
    """Description with action word and enough length -> no clarification needed."""
    assert needs_clarification(
        "добавь эндпоинт /api/users для получения списка пользователей", {}
    ) is False


def test_needs_clarification_custom_threshold():
    """Custom clarification_min_length from config respected."""
    # Has action word "fix" + long enough -> no clarification
    assert needs_clarification("fix the authentication bug in login", {"clarification_min_length": 50}) is False
    # Very short (< 15 chars) without action word -> always clarification
    assert needs_clarification("баг тут", {"clarification_min_length": 5}) is True


# ── generate_clarifying_questions tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_clarifying_questions():
    """Runner returns numbered questions -> result contains them."""
    mock_runner = AsyncMock(return_value="1. What exactly?\n2. What files?\n3. What result?")
    result = await generate_clarifying_questions("авторизация", runner=mock_runner)
    assert "1." in result
    assert "2." in result


@pytest.mark.asyncio
async def test_generate_clarifying_questions_fallback():
    """Runner raises -> returns default fallback questions."""
    mock_runner = AsyncMock(side_effect=RuntimeError("boom"))
    result = await generate_clarifying_questions("авторизация", runner=mock_runner)
    assert "Уточни задачу" in result
    assert "1." in result
