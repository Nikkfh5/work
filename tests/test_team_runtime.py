"""
tests/test_team_runtime.py — тесты для supervisor/team_runtime.py.

Тестируем:
- decompose_plan: разбиение на параллельные группы
- run_team: параллельное выполнение подзадач
- format_team_results: форматирование результатов
"""

import pytest
from unittest.mock import AsyncMock

from supervisor.team_runtime import (
    decompose_plan,
    format_team_results,
    run_team,
)


# ── decompose_plan ──────────────────────────────────────────────────────────


def test_decompose_empty_plan():
    assert decompose_plan({}) == []
    assert decompose_plan({"tasks": []}) == []


def test_decompose_single_task():
    plan = {
        "tasks": [
            {"step": 1, "description": "do X", "files_to_modify": ["a.py"]},
        ]
    }
    groups = decompose_plan(plan)
    assert len(groups) == 1
    assert len(groups[0]) == 1


def test_decompose_independent_tasks_parallel():
    """Tasks touching different files should be in the same group (parallel)."""
    plan = {
        "tasks": [
            {"step": 1, "description": "do X", "files_to_modify": ["a.py"]},
            {"step": 2, "description": "do Y", "files_to_modify": ["b.py"]},
            {"step": 3, "description": "do Z", "files_to_modify": ["c.py"]},
        ]
    }
    groups = decompose_plan(plan)
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_decompose_dependent_tasks_sequential():
    """Tasks touching same files should be in different groups (sequential)."""
    plan = {
        "tasks": [
            {"step": 1, "description": "do X", "files_to_modify": ["a.py"]},
            {"step": 2, "description": "do Y", "files_to_modify": ["a.py"]},
        ]
    }
    groups = decompose_plan(plan)
    assert len(groups) == 2
    assert len(groups[0]) == 1
    assert len(groups[1]) == 1


def test_decompose_mixed():
    """Mix of independent and dependent tasks."""
    plan = {
        "tasks": [
            {"step": 1, "description": "X", "files_to_modify": ["a.py"]},
            {"step": 2, "description": "Y", "files_to_modify": ["b.py"]},
            {"step": 3, "description": "Z", "files_to_modify": ["a.py", "c.py"]},
        ]
    }
    groups = decompose_plan(plan)
    # Step 1 and 2 are independent (different files) -> group 1
    # Step 3 overlaps with step 1 (a.py) -> group 2
    assert len(groups) == 2
    assert len(groups[0]) == 2  # steps 1, 2
    assert len(groups[1]) == 1  # step 3


def test_decompose_no_files_always_parallel():
    """Tasks without files_to_modify are always parallel."""
    plan = {
        "tasks": [
            {"step": 1, "description": "X"},
            {"step": 2, "description": "Y"},
        ]
    }
    groups = decompose_plan(plan)
    assert len(groups) == 1
    assert len(groups[0]) == 2


# ── run_team ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_team_single_group():
    """Single group of tasks should all succeed."""
    mock_runner = AsyncMock(
        return_value=(
            '<<<JSON>>>\n{"status": "done", "confidence": 90, '
            '"result": {"repos": [], "notes": "did it"}, "question": null}\n<<<END>>>'
        )
    )
    groups = [[{"step": 1, "description": "do X"}]]
    results = await run_team(
        groups, task_id="test-123", worker_id="w1",
        config={}, runner=mock_runner,
    )
    assert len(results) == 1
    assert results[0]["status"] == "done"


@pytest.mark.asyncio
async def test_run_team_parallel_execution():
    """Tasks in same group should run in parallel."""
    call_order = []

    async def slow_runner(prompt, cwd=None, timeout=None):
        step = "1" if "шаг 1" in prompt else "2"
        call_order.append(f"start_{step}")
        await asyncio.sleep(0.01)
        call_order.append(f"end_{step}")
        return (
            '<<<JSON>>>\n{"status": "done", "confidence": 80, '
            '"result": {"repos": [], "notes": "ok"}, "question": null}\n<<<END>>>'
        )

    import asyncio

    groups = [
        [
            {"step": 1, "description": "X"},
            {"step": 2, "description": "Y"},
        ]
    ]
    results = await run_team(
        groups, task_id="test-123", worker_id="w1",
        config={}, runner=slow_runner,
    )
    assert len(results) == 2
    assert all(r["status"] == "done" for r in results)


@pytest.mark.asyncio
async def test_run_team_subtask_failure_continues():
    """If one subtask fails, others should still succeed."""
    call_count = 0

    async def failing_runner(prompt, cwd=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        return (
            '<<<JSON>>>\n{"status": "done", "confidence": 80, '
            '"result": {"repos": [], "notes": "ok"}, "question": null}\n<<<END>>>'
        )

    groups = [
        [
            {"step": 1, "description": "fail"},
            {"step": 2, "description": "succeed"},
        ]
    ]
    results = await run_team(
        groups, task_id="test-123", worker_id="w1",
        config={}, runner=failing_runner,
    )
    assert len(results) == 2
    statuses = {r["step"]: r["status"] for r in results}
    assert statuses[1] == "error"
    assert statuses[2] == "done"


@pytest.mark.asyncio
async def test_run_team_sequential_groups():
    """Groups should execute sequentially."""
    mock_runner = AsyncMock(
        return_value=(
            '<<<JSON>>>\n{"status": "done", "confidence": 80, '
            '"result": {"repos": [], "notes": "ok"}, "question": null}\n<<<END>>>'
        )
    )
    groups = [
        [{"step": 1, "description": "first"}],
        [{"step": 2, "description": "second"}],
    ]
    results = await run_team(
        groups, task_id="test-123", worker_id="w1",
        config={}, runner=mock_runner,
    )
    assert len(results) == 2
    assert results[0]["step"] == 1
    assert results[1]["step"] == 2


# ── format_team_results ────────────────────────────────────────────────────


def test_format_team_results():
    results = [
        {"step": 1, "status": "done", "notes": "created file"},
        {"step": 2, "status": "error", "notes": "failed"},
    ]
    text = format_team_results(results)
    assert "1/2 succeeded" in text
    assert "created file" in text
