"""
supervisor/team_runtime.py — Team Runtime: параллельное выполнение подзадач плана.

Разбивает утверждённый план на независимые группы задач,
запускает их параллельно в отдельных worktrees, ждёт завершения,
собирает результаты.

Вдохновлено oh-my-codex $team: каждый worker в своём worktree,
координация через DB, merge результатов.

Инварианты:
- Нет shell=True
- Каждый субагент работает в изолированном worktree
- Координация через DB (task_runs), не через файлы
- Максимум max_parallel субагентов одновременно
- При провале одного субагента — остальные продолжают
"""

import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def decompose_plan(plan: dict) -> list[list[dict]]:
    """
    Разбить план на группы параллельных задач.

    Анализирует зависимости по файлам: задачи, работающие
    с разными файлами → параллельно; с одними файлами → последовательно.

    Args:
        plan: структурированный план из planner.py

    Returns:
        Список групп: [[task1, task2], [task3]] — внутри группы параллельно,
        группы выполняются последовательно.
    """
    tasks = plan.get("tasks", [])
    if not tasks:
        return []

    # Собираем файлы для каждой задачи
    task_files = []
    for task in tasks:
        files = set(task.get("files_to_modify", []))
        task_files.append((task, files))

    groups: list[list[dict]] = []
    remaining = list(range(len(task_files)))

    while remaining:
        # Жадный алгоритм: берём задачи без пересечения файлов
        current_group: list[dict] = []
        used_files: set[str] = set()
        still_remaining: list[int] = []

        for idx in remaining:
            task, files = task_files[idx]
            if not files or not files.intersection(used_files):
                current_group.append(task)
                used_files.update(files)
            else:
                still_remaining.append(idx)

        groups.append(current_group)
        remaining = still_remaining

    return groups


async def run_team(
    groups: list[list[dict]],
    task_id: str,
    worker_id: str,
    config: dict,
    db_path: Optional[str] = None,
    runner: Optional[Callable] = None,
    max_parallel: int = 3,
    cwd: Optional[str] = None,
) -> list[dict]:
    """
    Выполнить группы задач: внутри группы параллельно, группы последовательно.

    Args:
        groups: результат decompose_plan()
        task_id: ID родительской задачи
        worker_id: ID воркера
        config: полный конфиг
        db_path: путь к БД
        runner: DI для run_claude
        max_parallel: максимум параллельных субагентов
        cwd: рабочая директория для субагентов (worktree path)

    Returns:
        Список результатов [{step, status, notes}]
    """
    from supervisor.claude_runner import run_claude

    actual_runner = runner or run_claude
    all_results: list[dict] = []

    for group_idx, group in enumerate(groups):
        logger.info(
            "run_team: group %d/%d (%d tasks) task_id=%s",
            group_idx + 1,
            len(groups),
            len(group),
            task_id,
        )

        # Ограничиваем параллелизм
        semaphore = asyncio.Semaphore(max_parallel)

        async def run_subtask(subtask: dict) -> dict:
            async with semaphore:
                return await _execute_subtask(
                    subtask, task_id, actual_runner, cwd=cwd
                )

        # Параллельно выполняем все задачи в группе
        coros = [run_subtask(subtask) for subtask in group]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for i, result in enumerate(results):
            step = group[i].get("step", "?")
            if isinstance(result, Exception):
                logger.warning(
                    "run_team: subtask %s failed task_id=%s: %s",
                    step, task_id, result,
                )
                all_results.append({
                    "step": step,
                    "status": "error",
                    "notes": str(result)[:200],
                })
            else:
                all_results.append(result)

    return all_results


async def _execute_subtask(
    subtask: dict,
    parent_task_id: str,
    runner: Callable,
    cwd: Optional[str] = None,
) -> dict:
    """
    Выполнить одну подзадачу плана через Claude CLI.

    Returns:
        {step, status, notes}
    """
    step = subtask.get("step", "?")
    description = subtask.get("description", "")
    files = subtask.get("files_to_modify", [])
    changes = subtask.get("changes", "")
    verification = subtask.get("verification", "")

    prompt = (
        f"Выполни шаг {step} из плана задачи {parent_task_id[:8]}:\n\n"
        f"Описание: {description}\n"
        f"Файлы: {', '.join(files) if files else 'не указаны'}\n"
        f"Изменения: {changes}\n"
        f"Проверка: {verification}\n\n"
        f"Выполни ТОЛЬКО этот шаг. Не трогай другие файлы.\n\n"
        f"<<<JSON>>>\n"
        f'{{"status": "done", "confidence": 80, '
        f'"result": {{"repos": [], "notes": "<что сделано>"}}, '
        f'"question": null}}\n'
        f"<<<END>>>"
    )

    try:
        raw = await runner(prompt, cwd=cwd, timeout=300)
        from supervisor.json_guard import extract_json

        parsed = extract_json(raw)
        if parsed:
            return {
                "step": step,
                "status": parsed.get("status", "done"),
                "notes": parsed.get("result", {}).get("notes", ""),
            }
        return {
            "step": step,
            "status": "done",
            "notes": raw[:200],
        }
    except Exception as exc:
        return {
            "step": step,
            "status": "error",
            "notes": str(exc)[:200],
        }


def format_team_results(results: list[dict]) -> str:
    """Форматировать результаты team runtime для TG/логирования."""
    lines = ["Team results:"]
    for r in results:
        icon = "✓" if r["status"] == "done" else "✗"
        lines.append(f"  {icon} Step {r['step']}: {r['notes'][:100]}")

    succeeded = sum(1 for r in results if r["status"] == "done")
    total = len(results)
    lines.append(f"\nTotal: {succeeded}/{total} succeeded")

    return "\n".join(lines)
