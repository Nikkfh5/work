"""CLI утилита для управления задачами на базе Click."""

import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path

import click


def get_task_file(config: str | None) -> Path:
    """Определить путь к файлу задач из --config, env или дефолт."""
    if config:
        return Path(config)
    env_path = os.environ.get("TASK_FILE")
    if env_path:
        return Path(env_path)
    return Path("tasks.json")


def load_tasks(path: Path) -> list[dict]:
    """Загрузить задачи из JSON файла."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        click.echo(f"Ошибка: файл {path} содержит невалидный JSON: {e}", err=True)
        raise SystemExit(1)


def save_tasks(path: Path, tasks: list[dict]) -> None:
    """Сохранить задачи в JSON файл."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


@click.group()
@click.option(
    "--config", default=None, help="Путь к JSON файлу задач (или env TASK_FILE)"
)
@click.pass_context
def task(ctx, config):
    """Менеджер задач — CLI утилита."""
    ctx.ensure_object(dict)
    ctx.obj["task_file"] = get_task_file(config)


@task.command()
@click.argument("title")
@click.option(
    "--priority",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    help="Приоритет задачи",
)
@click.option(
    "--due",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Дедлайн (YYYY-MM-DD)",
)
@click.pass_context
def add(ctx, title, priority, due):
    """Добавить новую задачу."""
    task_file = ctx.obj["task_file"]
    tasks = load_tasks(task_file)

    task_id = str(uuid.uuid4())[:8]
    new_task = {
        "id": task_id,
        "title": title,
        "priority": priority,
        "due": due.strftime("%Y-%m-%d") if due else None,
        "status": "open",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tasks.append(new_task)
    save_tasks(task_file, tasks)
    click.echo(f"Задача добавлена: [{task_id}] {title}")


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@task.command("list")
@click.option(
    "--status",
    type=click.Choice(["all", "open", "done"]),
    default="all",
    help="Фильтр по статусу",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["priority", "due"]),
    default=None,
    help="Сортировка",
)
@click.pass_context
def list_tasks(ctx, status, sort_by):
    """Вывести таблицу задач."""
    task_file = ctx.obj["task_file"]
    tasks = load_tasks(task_file)

    if status != "all":
        tasks = [t for t in tasks if t["status"] == status]

    if sort_by == "priority":
        tasks.sort(key=lambda t: PRIORITY_ORDER.get(t["priority"], 99))
    elif sort_by == "due":
        tasks.sort(key=lambda t: t.get("due") or "9999-12-31")

    if not tasks:
        click.echo("Задач не найдено.")
        return

    click.echo(
        f"{'ID':<10} {'Статус':<8} {'Приоритет':<10} {'Дедлайн':<12} {'Название'}"
    )
    click.echo("-" * 60)
    for t in tasks:
        due_str = t.get("due") or "-"
        click.echo(
            f"{t['id']:<10} {t['status']:<8} {t['priority']:<10} {due_str:<12} {t['title']}"
        )


@task.command()
@click.argument("task_id")
@click.pass_context
def done(ctx, task_id):
    """Пометить задачу как выполненную."""
    task_file = ctx.obj["task_file"]
    tasks = load_tasks(task_file)

    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "done"
            save_tasks(task_file, tasks)
            click.echo(f"Задача [{task_id}] помечена как done.")
            return

    click.echo(f"Задача с ID {task_id} не найдена.", err=True)
    raise SystemExit(1)


@task.command()
@click.pass_context
def stats(ctx):
    """Статистика задач: total, done, overdue."""
    task_file = ctx.obj["task_file"]
    tasks = load_tasks(task_file)

    total = len(tasks)
    done_count = sum(1 for t in tasks if t["status"] == "done")
    today = date.today().isoformat()
    overdue = sum(
        1 for t in tasks if t["status"] == "open" and t.get("due") and t["due"] < today
    )

    click.echo(f"Всего:      {total}")
    click.echo(f"Выполнено:  {done_count}")
    click.echo(f"Просрочено: {overdue}")


if __name__ == "__main__":
    task()
