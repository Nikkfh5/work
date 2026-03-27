"""Tests for task_cli.py using click.testing.CliRunner."""

import json
import os

import pytest
from click.testing import CliRunner

from task_cli import cli, _load_tasks


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def task_file(tmp_path):
    return str(tmp_path / "tasks.json")


class TestAdd:
    def test_add_basic(self, runner, task_file):
        result = runner.invoke(cli, ["--config", task_file, "add", "Buy milk"])
        assert result.exit_code == 0
        assert "Added task #1" in result.output

        tasks = _load_tasks(task_file)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Buy milk"
        assert tasks[0]["priority"] == "medium"
        assert tasks[0]["status"] == "open"
        assert tasks[0]["due"] is None

    def test_add_with_priority(self, runner, task_file):
        result = runner.invoke(
            cli, ["--config", task_file, "add", "Urgent fix", "--priority", "high"]
        )
        assert result.exit_code == 0
        tasks = _load_tasks(task_file)
        assert tasks[0]["priority"] == "high"

    def test_add_with_due(self, runner, task_file):
        result = runner.invoke(
            cli, ["--config", task_file, "add", "Report", "--due", "2026-04-01"]
        )
        assert result.exit_code == 0
        tasks = _load_tasks(task_file)
        assert tasks[0]["due"] == "2026-04-01"

    def test_add_invalid_due_format(self, runner, task_file):
        result = runner.invoke(
            cli, ["--config", task_file, "add", "Bad date", "--due", "not-a-date"]
        )
        assert result.exit_code != 0
        assert "Invalid date format" in result.output

    def test_add_auto_increment(self, runner, task_file):
        runner.invoke(cli, ["--config", task_file, "add", "First"])
        runner.invoke(cli, ["--config", task_file, "add", "Second"])
        runner.invoke(cli, ["--config", task_file, "add", "Third"])

        tasks = _load_tasks(task_file)
        assert [t["id"] for t in tasks] == [1, 2, 3]

    def test_add_invalid_priority(self, runner, task_file):
        result = runner.invoke(
            cli, ["--config", task_file, "add", "Task", "--priority", "critical"]
        )
        assert result.exit_code != 0

    def test_add_with_all_options(self, runner, task_file):
        result = runner.invoke(
            cli,
            [
                "--config",
                task_file,
                "add",
                "Full task",
                "--priority",
                "low",
                "--due",
                "2026-12-31",
            ],
        )
        assert result.exit_code == 0
        tasks = _load_tasks(task_file)
        assert tasks[0]["title"] == "Full task"
        assert tasks[0]["priority"] == "low"
        assert tasks[0]["due"] == "2026-12-31"


class TestList:
    def _seed(self, runner, task_file):
        runner.invoke(
            cli,
            [
                "--config",
                task_file,
                "add",
                "Low task",
                "--priority",
                "low",
                "--due",
                "2026-05-01",
            ],
        )
        runner.invoke(
            cli,
            [
                "--config",
                task_file,
                "add",
                "High task",
                "--priority",
                "high",
                "--due",
                "2026-03-01",
            ],
        )
        runner.invoke(
            cli,
            [
                "--config",
                task_file,
                "add",
                "Medium task",
                "--priority",
                "medium",
                "--due",
                "2026-04-01",
            ],
        )

    def test_list_all(self, runner, task_file):
        self._seed(runner, task_file)
        result = runner.invoke(cli, ["--config", task_file, "list"])
        assert result.exit_code == 0
        assert "Low task" in result.output
        assert "High task" in result.output
        assert "Medium task" in result.output

    def test_list_empty(self, runner, task_file):
        result = runner.invoke(cli, ["--config", task_file, "list"])
        assert result.exit_code == 0
        assert "No tasks found" in result.output

    def test_list_filter_open(self, runner, task_file):
        self._seed(runner, task_file)
        runner.invoke(cli, ["--config", task_file, "done", "1"])

        result = runner.invoke(cli, ["--config", task_file, "list", "--status", "open"])
        assert "Low task" not in result.output
        assert "High task" in result.output

    def test_list_filter_done(self, runner, task_file):
        self._seed(runner, task_file)
        runner.invoke(cli, ["--config", task_file, "done", "2"])

        result = runner.invoke(cli, ["--config", task_file, "list", "--status", "done"])
        assert "High task" in result.output
        assert "Low task" not in result.output
        assert "Medium task" not in result.output

    def test_list_sort_priority(self, runner, task_file):
        self._seed(runner, task_file)
        result = runner.invoke(
            cli, ["--config", task_file, "list", "--sort", "priority"]
        )
        assert result.exit_code == 0
        lines = result.output.strip().split("\n")
        data_lines = [
            l for l in lines if l.strip() and not l.startswith("-") and "ID" not in l
        ]
        assert "high" in data_lines[0]
        assert "low" in data_lines[-1]

    def test_list_sort_due(self, runner, task_file):
        self._seed(runner, task_file)
        result = runner.invoke(cli, ["--config", task_file, "list", "--sort", "due"])
        assert result.exit_code == 0
        lines = result.output.strip().split("\n")
        data_lines = [
            l for l in lines if l.strip() and not l.startswith("-") and "ID" not in l
        ]
        assert "2026-03-01" in data_lines[0]
        assert "2026-05-01" in data_lines[-1]

    def test_list_no_match(self, runner, task_file):
        self._seed(runner, task_file)
        result = runner.invoke(cli, ["--config", task_file, "list", "--status", "done"])
        assert "No tasks match" in result.output


class TestDone:
    def test_done_marks_task(self, runner, task_file):
        runner.invoke(cli, ["--config", task_file, "add", "Task to do"])
        result = runner.invoke(cli, ["--config", task_file, "done", "1"])
        assert result.exit_code == 0
        assert "marked as done" in result.output

        tasks = _load_tasks(task_file)
        assert tasks[0]["status"] == "done"

    def test_done_already_done(self, runner, task_file):
        runner.invoke(cli, ["--config", task_file, "add", "Task"])
        runner.invoke(cli, ["--config", task_file, "done", "1"])
        result = runner.invoke(cli, ["--config", task_file, "done", "1"])
        assert "already done" in result.output

    def test_done_not_found(self, runner, task_file):
        runner.invoke(cli, ["--config", task_file, "add", "Task"])
        result = runner.invoke(cli, ["--config", task_file, "done", "999"])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestStats:
    def test_stats_empty(self, runner, task_file):
        result = runner.invoke(cli, ["--config", task_file, "stats"])
        assert result.exit_code == 0
        assert "Total:   0" in result.output
        assert "Done:    0" in result.output
        assert "Overdue: 0" in result.output

    def test_stats_with_tasks(self, runner, task_file):
        runner.invoke(cli, ["--config", task_file, "add", "T1"])
        runner.invoke(cli, ["--config", task_file, "add", "T2"])
        runner.invoke(cli, ["--config", task_file, "add", "T3"])
        runner.invoke(cli, ["--config", task_file, "done", "2"])

        result = runner.invoke(cli, ["--config", task_file, "stats"])
        assert "Total:   3" in result.output
        assert "Done:    1" in result.output

    def test_stats_overdue(self, runner, task_file):
        runner.invoke(
            cli,
            ["--config", task_file, "add", "Overdue task", "--due", "2020-01-01"],
        )
        runner.invoke(
            cli,
            ["--config", task_file, "add", "Future task", "--due", "2099-12-31"],
        )
        runner.invoke(
            cli,
            ["--config", task_file, "add", "Done overdue", "--due", "2020-06-01"],
        )
        runner.invoke(cli, ["--config", task_file, "done", "3"])

        result = runner.invoke(cli, ["--config", task_file, "stats"])
        assert "Total:   3" in result.output
        assert "Done:    1" in result.output
        assert "Overdue: 1" in result.output  # only open overdue


class TestEnvConfig:
    def test_env_task_file(self, runner, task_file):
        result = runner.invoke(cli, ["add", "Env task"], env={"TASK_FILE": task_file})
        assert result.exit_code == 0
        tasks = _load_tasks(task_file)
        assert len(tasks) == 1

    def test_config_overrides_env(self, runner, tmp_path):
        env_file = str(tmp_path / "env.json")
        config_file = str(tmp_path / "config.json")

        result = runner.invoke(
            cli,
            ["--config", config_file, "add", "Config task"],
            env={"TASK_FILE": env_file},
        )
        assert result.exit_code == 0
        assert os.path.exists(config_file)
        assert not os.path.exists(env_file)
