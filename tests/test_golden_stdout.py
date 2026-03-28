"""
tests/test_golden_stdout.py — тесты на реальных выводах Claude CLI.

Golden stdout файлы — настоящие ответы Claude из debug-сессий.
Проверяем что json_guard корректно парсит и валидирует их.
"""

import os
from pathlib import Path

import pytest

from supervisor.json_guard import (
    extract_json,
    validate_reviewer_schema,
    validate_worker_schema,
)

GOLDEN_DIR = Path(__file__).parent / "golden_stdout"


def _load_golden(filename: str) -> str:
    """Загрузить golden stdout файл."""
    path = GOLDEN_DIR / filename
    if not path.exists():
        pytest.skip(f"Golden file not found: {path}")
    return path.read_text(encoding="utf-8")


# ── Worker outputs ──────────────────────────────────────────────────────────


class TestGoldenWorkerOutputs:
    """Реальные выводы worker Claude CLI."""

    def test_worker_palindrome_valid_json(self):
        """Worker is_palindrome — должен распарситься и пройти валидацию."""
        raw = _load_golden("job1_worker_90532ced_worker_20260327_224521.txt")
        parsed = extract_json(raw)
        assert parsed is not None, f"extract_json returned None for palindrome output"
        valid, err = validate_worker_schema(parsed)
        assert valid, f"Schema invalid: {err}"
        assert parsed["status"] == "done"
        assert 0 <= parsed["confidence"] <= 100

    def test_worker_validators_valid_json(self):
        """Worker validators.py — должен распарситься."""
        raw = _load_golden("job1_worker_ca6f85d6_worker_20260327_235912.txt")
        parsed = extract_json(raw)
        assert parsed is not None
        valid, err = validate_worker_schema(parsed)
        assert valid, f"Schema invalid: {err}"

    def test_worker_system_setup_valid_json(self):
        """Worker system setup task — реальный output."""
        raw = _load_golden("job1_worker_00f88dd8_worker_20260302_220915.txt")
        parsed = extract_json(raw)
        assert parsed is not None
        valid, err = validate_worker_schema(parsed)
        assert valid, f"Schema invalid: {err}"

    def test_worker_hello_valid_json(self):
        """Worker hello.py task — second attempt output."""
        raw = _load_golden("job1_worker_c1cad345_worker_20260228_234929.txt")
        parsed = extract_json(raw)
        assert parsed is not None
        valid, err = validate_worker_schema(parsed)
        assert valid, f"Schema invalid: {err}"

    def test_worker_invalid_json_output(self):
        """Worker death.txt — output without valid JSON (json_invalid case)."""
        raw = _load_golden("job1_worker_e47a6b1a_worker_20260228_234307.txt")
        parsed = extract_json(raw)
        # This task had json_invalid error — extract_json may or may not find JSON
        # The key test: it should NOT crash
        if parsed is not None:
            # If it did find something, it should be a dict
            assert isinstance(parsed, dict)


# ── Reviewer outputs ────────────────────────────────────────────────────────


class TestGoldenReviewerOutputs:
    """Реальные выводы reviewer Claude CLI."""

    def test_reviewer_needs_changes_valid_json(self):
        """Reviewer NEEDS_CHANGES — palindrome task."""
        raw = _load_golden("job1_reviewer_90532ced_reviewer_20260327_224554.txt")
        parsed = extract_json(raw)
        assert parsed is not None
        valid, err = validate_reviewer_schema(parsed)
        assert valid, f"Schema invalid: {err}"
        assert parsed["verdict"] == "NEEDS_CHANGES"
        assert isinstance(parsed.get("issues"), list)
        assert len(parsed["issues"]) > 0

    def test_reviewer_validators_valid_json(self):
        """Reviewer for validators.py — NEEDS_CHANGES with real issues."""
        raw = _load_golden("job1_reviewer_ca6f85d6_reviewer_20260328_000058.txt")
        parsed = extract_json(raw)
        assert parsed is not None
        valid, err = validate_reviewer_schema(parsed)
        assert valid, f"Schema invalid: {err}"
        assert parsed["verdict"] == "NEEDS_CHANGES"


# ── Edge cases (synthetic from real patterns) ───────────────────────────────


class TestGoldenEdgeCases:
    """Edge cases вдохновлённые реальными outputs."""

    def test_two_json_blocks_picks_agent_json(self):
        """Если Claude выведет два JSON — берём тот что похож на agent response."""
        raw = """
I'll think about this...
{"debug": "intermediate", "step": 1}
Here's my result:
<<<JSON>>>
{"status": "done", "confidence": 85, "result": {"repos": [], "notes": "ok"}, "question": null}
<<<END>>>
"""
        parsed = extract_json(raw)
        assert parsed is not None
        assert parsed["status"] == "done"
        assert parsed["confidence"] == 85

    def test_json_without_markers_picks_valid(self):
        """Без маркеров — fallback regex должен найти agent JSON."""
        raw = """
Some preamble text.
{"status": "done", "confidence": 90, "result": {"repos": [{"alias": "api", "changed_files": ["x.py"]}], "notes": "created"}, "question": null}
Some epilogue.
"""
        parsed = extract_json(raw)
        assert parsed is not None
        valid, err = validate_worker_schema(parsed)
        assert valid, f"Schema invalid: {err}"

    def test_markers_with_invalid_json_inside(self):
        """Маркеры есть, но JSON внутри битый — fallback regex."""
        raw = """
<<<JSON>>>
{invalid json here!!!
<<<END>>>
Also here's a valid one:
{"status": "blocked", "confidence": 10, "result": {"repos": [], "notes": ""}, "question": "what repo?"}
"""
        parsed = extract_json(raw)
        assert parsed is not None
        assert parsed["status"] == "blocked"

    def test_empty_output(self):
        """Пустой output — не крашится."""
        assert extract_json("") is None
        assert extract_json("   ") is None

    def test_no_json_at_all(self):
        """Текст без JSON — не крашится."""
        assert extract_json("Just some text without any JSON") is None
