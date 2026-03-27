"""CLI tool for task management using Click.

Stores tasks in a JSON file with auto-increment IDs.
File path configurable via TASK_FILE env var or --config option.
"""

import json
import os
from datetime import date, datetime

import click


DEFAULT_TASK_FILE = "tasks.json"

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _get_task_file(config):
    """Resolve task file path: --config > TASK_FILE env > default."""
    if config:
        return config
    return os.environ.get("TASK_FILE", DEFAULT_TASK_FILE)


def _load_tasks(path):
    """Load tasks from JSON file. Returns empty list if file doesn't exist."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_tasks(path, tasks):
    """Save tasks list to JSON file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)


def _next_id(tasks):
    """Get next auto-increment ID."""
    if not tasks:
        return 1
    return max(t["id"] for t in tasks) + 1


@click.group()
@click.option("--config", default=None, help="Path to tasks JSON file.")
@click.pass_context
def cli(ctx, config):
    """Task manager CLI."""
    ctx.ensure_object(dict)
    ctx.obj["task_file"] = _get_task_file(config)


@cli.command()
@click.argument("title")
@click.option(
    "--priority",
    type=click.Choice(["low", "medium", "high"], case_sensitive=False),
    default="medium",
    help="Task priority.",
)
@click.option(
    "--due",
    default=None,
    help="Due date in YYYY-MM-DD format.",
)
@click.pass_context
def add(ctx, title, priority, due):
    """Add a new task."""
    if due is not None:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            click.echo("Error: Invalid date format. Use YYYY-MM-DD.")
            ctx.exit(1)
            return

    path = ctx.obj["task_file"]
    tasks = _load_tasks(path)
    task_id = _next_id(tasks)

    task = {
        "id": task_id,
        "title": title,
        "priority": priority.lower(),
        "due": due,
        "status": "open",
        "created": date.today().isoformat(),
    }
    tasks.append(task)
    _save_tasks(path, tasks)
    click.echo(f"Added task #{task_id}: {title}")


@cli.command("list")
@click.option(
    "--status",
    type=click.Choice(["all", "open", "done"], case_sensitive=False),
    default="all",
    help="Filter by status.",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["priority", "due"], case_sensitive=False),
    default=None,
    help="Sort tasks by field.",
)
@click.pass_context
def list_tasks(ctx, status, sort_by):
    """List tasks as a table."""
    path = ctx.obj["task_file"]
    tasks = _load_tasks(path)

    if not tasks:
        click.echo("No tasks found.")
        return

    if status != "all":
        tasks = [t for t in tasks if t["status"] == status.lower()]

    if sort_by == "priority":
        tasks.sort(key=lambda t: PRIORITY_ORDER.get(t["priority"], 99))
    elif sort_by == "due":
        tasks.sort(key=lambda t: t.get("due") or "9999-99-99")

    if not tasks:
        click.echo("No tasks match the filter.")
        return

    header = f"{'ID':>4}  {'Status':<6}  {'Pri':<6}  {'Due':<12}  {'Title'}"
    click.echo(header)
    click.echo("-" * len(header))
    for t in tasks:
        due_str = t.get("due") or "-"
        click.echo(
            f"{t['id']:>4}  {t['status']:<6}  {t['priority']:<6}  {due_str:<12}  {t['title']}"
        )


@cli.command()
@click.argument("task_id", type=int)
@click.pass_context
def done(ctx, task_id):
    """Mark a task as done."""
    path = ctx.obj["task_file"]
    tasks = _load_tasks(path)

    for t in tasks:
        if t["id"] == task_id:
            if t["status"] == "done":
                click.echo(f"Task #{task_id} is already done.")
                return
            t["status"] = "done"
            _save_tasks(path, tasks)
            click.echo(f"Task #{task_id} marked as done.")
            return

    click.echo(f"Error: Task #{task_id} not found.")
    ctx.exit(1)


@cli.command()
@click.pass_context
def stats(ctx):
    """Show task statistics: total, done, overdue."""
    path = ctx.obj["task_file"]
    tasks = _load_tasks(path)

    total = len(tasks)
    done_count = sum(1 for t in tasks if t["status"] == "done")
    today = date.today().isoformat()
    overdue = sum(
        1 for t in tasks if t["status"] == "open" and t.get("due") and t["due"] < today
    )

    click.echo(f"Total:   {total}")
    click.echo(f"Done:    {done_count}")
    click.echo(f"Overdue: {overdue}")


if __name__ == "__main__":
    cli()
