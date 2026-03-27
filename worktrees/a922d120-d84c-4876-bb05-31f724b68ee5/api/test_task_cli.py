"""Тесты для task_cli.py с click.testing.CliRunner."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from task_cli import task, load_tasks, save_tasks


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def task_file(tmp_path):
    return tmp_path / "tasks.json"


@pytest.fixture
def config_args(task_file):
    return ["--config", str(task_file)]


# ── load_tasks / save_tasks ────────────────────────────────


class TestLoadSave:
    def test_load_empty_file(self, tmp_path):
        path = tmp_path / "empty.json"
        assert load_tasks(path) == []

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "rt.json"
        data = [{"id": "abc", "title": "test", "status": "open"}]
        save_tasks(path, data)
        assert load_tasks(path) == data

    def test_load_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(SystemExit):
            load_tasks(path)

    def test_load_invalid_json_via_cli(self, runner, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json!", encoding="utf-8")
        result = runner.invoke(task, ["--config", str(bad_file), "list"])
        assert result.exit_code != 0
        assert "невалидный JSON" in result.output

    def test_save_unicode(self, tmp_path):
        path = tmp_path / "uni.json"
        data = [{"title": "Задача с юникодом 🎉"}]
        save_tasks(path, data)
        loaded = load_tasks(path)
        assert loaded[0]["title"] == "Задача с юникодом 🎉"


# ── task add ────────────────────────────────────────────────


class TestAdd:
    def test_add_basic(self, runner, config_args, task_file):
        result = runner.invoke(task, [*config_args, "add", "Buy milk"])
        assert result.exit_code == 0
        assert "Задача добавлена" in result.output

        tasks = load_tasks(task_file)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Buy milk"
        assert tasks[0]["status"] == "open"
        assert tasks[0]["priority"] == "medium"

    def test_add_with_priority(self, runner, config_args, task_file):
        result = runner.invoke(
            task, [*config_args, "add", "Urgent", "--priority", "high"]
        )
        assert result.exit_code == 0
        tasks = load_tasks(task_file)
        assert tasks[0]["priority"] == "high"

    def test_add_with_due(self, runner, config_args, task_file):
        result = runner.invoke(
            task, [*config_args, "add", "Report", "--due", "2026-04-15"]
        )
        assert result.exit_code == 0
        tasks = load_tasks(task_file)
        assert tasks[0]["due"] == "2026-04-15"

    def test_add_with_all_options(self, runner, config_args, task_file):
        result = runner.invoke(
            task,
            [
                *config_args,
                "add",
                "Deploy",
                "--priority",
                "high",
                "--due",
                "2026-05-01",
            ],
        )
        assert result.exit_code == 0
        tasks = load_tasks(task_file)
        assert tasks[0]["priority"] == "high"
        assert tasks[0]["due"] == "2026-05-01"
        assert tasks[0]["title"] == "Deploy"

    def test_add_invalid_priority(self, runner, config_args):
        result = runner.invoke(
            task, [*config_args, "add", "Fail", "--priority", "urgent"]
        )
        assert result.exit_code != 0

    def test_add_invalid_due_format(self, runner, config_args):
        result = runner.invoke(
            task, [*config_args, "add", "Fail", "--due", "15-04-2026"]
        )
        assert result.exit_code != 0

    def test_add_multiple_tasks(self, runner, config_args, task_file):
        runner.invoke(task, [*config_args, "add", "Task 1"])
        runner.invoke(task, [*config_args, "add", "Task 2"])
        runner.invoke(task, [*config_args, "add", "Task 3"])
        tasks = load_tasks(task_file)
        assert len(tasks) == 3
        assert [t["title"] for t in tasks] == ["Task 1", "Task 2", "Task 3"]


# ── task list ───────────────────────────────────────────────


class TestList:
    def _seed(self, task_file):
        tasks = [
            {
                "id": "aaa",
                "title": "Low task",
                "priority": "low",
                "due": "2026-06-01",
                "status": "open",
                "created": "2026-03-28 10:00:00",
            },
            {
                "id": "bbb",
                "title": "High task",
                "priority": "high",
                "due": "2026-04-01",
                "status": "done",
                "created": "2026-03-28 10:01:00",
            },
            {
                "id": "ccc",
                "title": "Medium task",
                "priority": "medium",
                "due": None,
                "status": "open",
                "created": "2026-03-28 10:02:00",
            },
        ]
        save_tasks(task_file, tasks)

    def test_list_all(self, runner, config_args, task_file):
        self._seed(task_file)
        result = runner.invoke(task, [*config_args, "list"])
        assert result.exit_code == 0
        assert "Low task" in result.output
        assert "High task" in result.output
        assert "Medium task" in result.output

    def test_list_filter_open(self, runner, config_args, task_file):
        self._seed(task_file)
        result = runner.invoke(task, [*config_args, "list", "--status", "open"])
        assert "Low task" in result.output
        assert "High task" not in result.output
        assert "Medium task" in result.output

    def test_list_filter_done(self, runner, config_args, task_file):
        self._seed(task_file)
        result = runner.invoke(task, [*config_args, "list", "--status", "done"])
        assert "High task" in result.output
        assert "Low task" not in result.output

    def test_list_sort_priority(self, runner, config_args, task_file):
        self._seed(task_file)
        result = runner.invoke(task, [*config_args, "list", "--sort", "priority"])
        lines = result.output.strip().split("\n")
        data_lines = [l for l in lines if l and not l.startswith("-") and "ID" not in l]
        assert "high" in data_lines[0]
        assert "low" in data_lines[-1]

    def test_list_sort_due(self, runner, config_args, task_file):
        self._seed(task_file)
        result = runner.invoke(task, [*config_args, "list", "--sort", "due"])
        lines = result.output.strip().split("\n")
        data_lines = [l for l in lines if l and not l.startswith("-") and "ID" not in l]
        # 2026-04-01 раньше 2026-06-01, None → конец
        assert "bbb" in data_lines[0]
        assert "ccc" in data_lines[-1]

    def test_list_empty(self, runner, config_args, task_file):
        result = runner.invoke(task, [*config_args, "list"])
        assert "не найдено" in result.output

    def test_list_no_file(self, runner, tmp_path):
        """Список задач когда файл ещё не создан."""
        missing = tmp_path / "nonexistent.json"
        result = runner.invoke(task, ["--config", str(missing), "list"])
        assert result.exit_code == 0
        assert "не найдено" in result.output


# ── task done ───────────────────────────────────────────────


class TestDone:
    def test_done_marks_task(self, runner, config_args, task_file):
        tasks = [
            {
                "id": "xyz1",
                "title": "Finish",
                "priority": "medium",
                "due": None,
                "status": "open",
                "created": "2026-03-28 10:00:00",
            },
        ]
        save_tasks(task_file, tasks)

        result = runner.invoke(task, [*config_args, "done", "xyz1"])
        assert result.exit_code == 0
        assert "done" in result.output

        updated = load_tasks(task_file)
        assert updated[0]["status"] == "done"

    def test_done_nonexistent_id(self, runner, config_args, task_file):
        save_tasks(task_file, [])
        result = runner.invoke(task, [*config_args, "done", "nope"])
        assert result.exit_code != 0
        assert "не найдена" in result.output

    def test_done_only_marks_correct_task(self, runner, config_args, task_file):
        tasks = [
            {
                "id": "t1",
                "title": "A",
                "priority": "low",
                "due": None,
                "status": "open",
                "created": "2026-03-28 10:00:00",
            },
            {
                "id": "t2",
                "title": "B",
                "priority": "high",
                "due": None,
                "status": "open",
                "created": "2026-03-28 10:01:00",
            },
        ]
        save_tasks(task_file, tasks)

        runner.invoke(task, [*config_args, "done", "t1"])
        updated = load_tasks(task_file)
        assert updated[0]["status"] == "done"
        assert updated[1]["status"] == "open"


# ── task stats ──────────────────────────────────────────────


class TestStats:
    def test_stats_empty(self, runner, config_args, task_file):
        result = runner.invoke(task, [*config_args, "stats"])
        assert result.exit_code == 0
        assert "Всего:      0" in result.output
        assert "Выполнено:  0" in result.output
        assert "Просрочено: 0" in result.output

    def test_stats_counts(self, runner, config_args, task_file):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        tasks = [
            {
                "id": "1",
                "title": "A",
                "priority": "low",
                "due": yesterday,
                "status": "open",
                "created": "2026-03-28",
            },
            {
                "id": "2",
                "title": "B",
                "priority": "high",
                "due": tomorrow,
                "status": "open",
                "created": "2026-03-28",
            },
            {
                "id": "3",
                "title": "C",
                "priority": "medium",
                "due": yesterday,
                "status": "done",
                "created": "2026-03-28",
            },
        ]
        save_tasks(task_file, tasks)

        result = runner.invoke(task, [*config_args, "stats"])
        assert "Всего:      3" in result.output
        assert "Выполнено:  1" in result.output
        assert "Просрочено: 1" in result.output  # только open + просроченные

    def test_stats_no_overdue_if_done(self, runner, config_args, task_file):
        """Выполненные задачи с прошедшим дедлайном НЕ считаются просроченными."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tasks = [
            {
                "id": "1",
                "title": "A",
                "priority": "low",
                "due": yesterday,
                "status": "done",
                "created": "2026-03-28",
            },
        ]
        save_tasks(task_file, tasks)

        result = runner.invoke(task, [*config_args, "stats"])
        assert "Просрочено: 0" in result.output

    def test_stats_no_overdue_without_due(self, runner, config_args, task_file):
        """Задачи без дедлайна не считаются просроченными."""
        tasks = [
            {
                "id": "1",
                "title": "A",
                "priority": "low",
                "due": None,
                "status": "open",
                "created": "2026-03-28",
            },
        ]
        save_tasks(task_file, tasks)

        result = runner.invoke(task, [*config_args, "stats"])
        assert "Просрочено: 0" in result.output


# ── env TASK_FILE ───────────────────────────────────────────


class TestEnvConfig:
    def test_env_task_file(self, runner, task_file):
        result = runner.invoke(
            task,
            ["add", "From env"],
            env={"TASK_FILE": str(task_file)},
        )
        assert result.exit_code == 0
        tasks = load_tasks(task_file)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "From env"
