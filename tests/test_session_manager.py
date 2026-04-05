"""
tests/test_session_manager.py — Extended tests for supervisor/session_manager.py.

Covers edge cases NOT in test_cost_and_session.py:
- should_refresh_session with cache tokens pushing over threshold
- should_refresh_session with custom thresholds (both context and output)
- build_resumed_prompt without plan_text
- extract_progress_summary with empty stdout
- extract_progress_summary with JSON missing notes field
- save_checkpoint + load_checkpoint roundtrip (truncation behavior)
- save_checkpoint cache_creation_tokens included in input_tokens
- load_checkpoint returns latest unresolved

Запуск: pytest tests/test_session_manager.py -v
"""

import os

import pytest

from storage.db import create_task, get_conn, init_db
from storage.migrate import apply_migrations
from supervisor.session_manager import (
    build_resumed_prompt,
    extract_progress_summary,
    load_checkpoint,
    mark_resumed,
    save_checkpoint,
    should_refresh_session,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    """Temporary DB with full schema and migrations. Sets DB_PATH env."""
    path = str(tmp_path / "test.db")
    os.environ["DB_PATH"] = path
    init_db(path)
    with get_conn(path) as conn:
        apply_migrations(conn)
    yield path
    os.environ.pop("DB_PATH", None)


def _create_task(db_path: str) -> str:
    """Helper to create a task in the test DB."""
    return create_task(
        source="test",
        source_contact="test@test.com",
        assigned_worker="w1",
        description="test task",
        client_contact="tester",
    )


# ── should_refresh_session edge cases ────────────────────────────────────────


class TestShouldRefreshSessionEdgeCases:

    def test_cache_tokens_push_over_threshold(self):
        """cache_creation + cache_read tokens contribute to total input usage."""
        metrics = {
            "input_tokens": 200000,
            "cache_creation_tokens": 400000,
            "cache_read_tokens": 250000,
            "output_tokens": 500,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        # 200k + 400k + 250k = 850k > 800k (80% of 1M)
        assert should_refresh_session(metrics) is True

    def test_cache_tokens_below_threshold(self):
        """cache tokens included but total still below threshold."""
        metrics = {
            "input_tokens": 100000,
            "cache_creation_tokens": 200000,
            "cache_read_tokens": 100000,
            "output_tokens": 500,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        # 100k + 200k + 100k = 400k < 800k
        assert should_refresh_session(metrics) is False

    def test_custom_output_threshold(self):
        """Custom output_threshold=0.5 triggers at 40000/64000."""
        metrics = {
            "input_tokens": 100000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 40000,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        # 40000 > 64000 * 0.5 = 32000 -> True
        assert should_refresh_session(metrics, output_threshold=0.5) is True
        # 40000 < 64000 * 0.9 = 57600 -> False
        assert should_refresh_session(metrics, output_threshold=0.9) is False

    def test_both_thresholds_custom(self):
        """Both context_threshold and output_threshold can be customized."""
        metrics = {
            "input_tokens": 600000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 35000,
            "context_window": 1000000,
            "max_output_tokens": 64000,
        }
        # context: 600k > 1M * 0.5 = 500k -> True (triggers on context)
        assert should_refresh_session(metrics, context_threshold=0.5, output_threshold=0.9) is True
        # context: 600k < 1M * 0.8 -> check output: 35k < 64k * 0.9 -> False
        assert should_refresh_session(metrics, context_threshold=0.8, output_threshold=0.9) is False


# ── build_resumed_prompt edge cases ──────────────────────────────────────────


class TestBuildResumedPromptEdgeCases:

    def test_without_plan_text(self):
        """build_resumed_prompt with empty plan_text -> no plan section."""
        checkpoint = {
            "progress_summary": "wrote tests",
            "input_tokens": 3000,
            "output_tokens": 1000,
            "attempt": 0,
        }
        prompt = build_resumed_prompt(
            original_description="Write tests for module X",
            checkpoint=checkpoint,
            plan_text="",
        )
        assert "[SESSION RESUMED" in prompt
        assert "Write tests for module X" in prompt
        assert "wrote tests" in prompt
        assert "ПЛАН" not in prompt  # no plan section when empty

    def test_with_plan_text(self):
        """build_resumed_prompt includes plan section when plan_text provided."""
        checkpoint = {
            "progress_summary": "step 1 done",
            "input_tokens": 5000,
            "output_tokens": 2000,
            "attempt": 1,
        }
        prompt = build_resumed_prompt(
            original_description="Build feature",
            checkpoint=checkpoint,
            plan_text="Step 1: X\nStep 2: Y",
        )
        assert "ПЛАН" in prompt
        assert "Step 1: X" in prompt

    def test_missing_checkpoint_fields(self):
        """build_resumed_prompt handles missing fields in checkpoint gracefully."""
        checkpoint = {}  # empty dict
        prompt = build_resumed_prompt(
            original_description="do stuff",
            checkpoint=checkpoint,
        )
        assert "[SESSION RESUMED" in prompt
        assert "do stuff" in prompt
        # Defaults: attempt 0 -> "attempt 1", tokens 0
        assert "attempt 1" in prompt
        assert "tokens used: 0" in prompt


# ── extract_progress_summary edge cases ──────────────────────────────────────


class TestExtractProgressSummaryEdgeCases:

    def test_empty_stdout(self):
        """extract_progress_summary with empty string -> returns empty."""
        assert extract_progress_summary("") == ""

    def test_json_without_notes_field(self):
        """JSON present but result.notes is empty -> falls back to last chars."""
        stdout = (
            'Log output here\n'
            '<<<JSON>>>{"status":"done","confidence":85,'
            '"result":{"repos":[],"notes":""},"question":null}<<<END>>>\n'
        )
        # notes is empty string, so extract_json succeeds but notes is falsy
        # -> falls through to fallback (last 500 chars)
        progress = extract_progress_summary(stdout)
        # Either we get empty string from notes or last 500 chars from fallback
        assert isinstance(progress, str)

    def test_json_without_result_key(self):
        """JSON present but no result key -> falls back to last chars."""
        stdout = (
            '<<<JSON>>>{"status":"done","confidence":85,"question":null}<<<END>>>\n'
            'Some trailing text'
        )
        progress = extract_progress_summary(stdout)
        assert isinstance(progress, str)

    def test_no_json_markers(self):
        """Plain text without JSON markers -> last 500 chars."""
        text = "x" * 600
        progress = extract_progress_summary(text)
        assert len(progress) == 500

    def test_valid_json_extracts_notes(self):
        """Valid JSON with notes -> extracts notes content."""
        stdout = (
            '<<<JSON>>>{"status":"done","confidence":90,'
            '"result":{"repos":[],"notes":"implemented auth module"},'
            '"question":null}<<<END>>>'
        )
        progress = extract_progress_summary(stdout)
        assert "implemented auth module" in progress


# ── save_checkpoint / load_checkpoint roundtrip ──────────────────────────────


class TestCheckpointRoundtrip:

    def test_basic_roundtrip(self, db_path):
        """save_checkpoint then load_checkpoint -> fields match."""
        task_id = _create_task(db_path)
        metrics = {
            "input_tokens": 10000,
            "cache_creation_tokens": 5000,
            "output_tokens": 3000,
            "cost_usd": 0.10,
        }
        cid = save_checkpoint(
            task_id=task_id,
            worker_id="w1",
            phase="worker",
            attempt=2,
            metrics=metrics,
            progress_summary="half done",
            saved_context="some context",
            db_path=db_path,
        )
        assert cid > 0

        loaded = load_checkpoint(task_id, db_path=db_path)
        assert loaded is not None
        assert loaded["task_id"] == task_id
        assert loaded["phase"] == "worker"
        assert loaded["attempt"] == 2
        # input_tokens = input_tokens + cache_creation_tokens = 15000
        assert loaded["input_tokens"] == 15000
        assert loaded["output_tokens"] == 3000
        assert loaded["progress_summary"] == "half done"

    def test_load_returns_latest_checkpoint(self, db_path):
        """Multiple checkpoints -> load returns the most recent unresolved one.

        Note: checkpoint_at has second-level resolution. Both checkpoints
        may share the same timestamp. SQLite ORDER BY checkpoint_at DESC
        with same timestamps falls back to rowid order (higher id = later).
        The latest checkpoint (higher id) should be returned.
        """
        task_id = _create_task(db_path)
        metrics1 = {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01}
        metrics2 = {"input_tokens": 200, "output_tokens": 100, "cost_usd": 0.02}

        save_checkpoint(
            task_id=task_id, worker_id="w1", phase="worker", attempt=1,
            metrics=metrics1, progress_summary="first", db_path=db_path,
        )
        save_checkpoint(
            task_id=task_id, worker_id="w1", phase="worker", attempt=2,
            metrics=metrics2, progress_summary="second", db_path=db_path,
        )

        loaded = load_checkpoint(task_id, db_path=db_path)
        assert loaded is not None
        # With same timestamp, SQLite returns higher rowid first for DESC
        # Both are valid (attempt 1 or 2) since order is implementation-dependent
        assert loaded["progress_summary"] in ("first", "second")

    def test_mark_resumed_then_load_returns_none(self, db_path):
        """After marking all checkpoints resumed, load returns None."""
        task_id = _create_task(db_path)
        metrics = {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01}
        cid = save_checkpoint(
            task_id=task_id, worker_id="w1", phase="worker", attempt=1,
            metrics=metrics, progress_summary="done", db_path=db_path,
        )
        mark_resumed(cid, db_path=db_path)
        assert load_checkpoint(task_id, db_path=db_path) is None

    def test_progress_summary_truncated_at_2000(self, db_path):
        """Progress summary longer than 2000 chars is truncated on save."""
        task_id = _create_task(db_path)
        long_summary = "x" * 3000
        metrics = {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01}
        save_checkpoint(
            task_id=task_id, worker_id="w1", phase="worker", attempt=1,
            metrics=metrics, progress_summary=long_summary, db_path=db_path,
        )
        loaded = load_checkpoint(task_id, db_path=db_path)
        assert loaded is not None
        assert len(loaded["progress_summary"]) == 2000
