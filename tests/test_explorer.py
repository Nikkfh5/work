"""
tests/test_explorer.py — тесты для explorer (read-only агент).

Тестируем:
- _build_explorer_prompt: формат промпта для explorer
- deliver_stage report mode: результат в TG без push
"""

from supervisor.stages.execute import _build_explorer_prompt


def test_explorer_prompt_read_only():
    """Explorer prompt should contain read-only instructions."""
    prompt = _build_explorer_prompt(
        description="найди где хранятся конфиги",
        task_id="test-123",
        repos_context=[{"alias": "api", "path": "/workspace/api"}],
    )
    assert "read-only" in prompt.lower() or "ЧИТАТЬ" in prompt
    assert "НЕ изменяй" in prompt
    assert "explorer" in prompt.lower()
    assert "api" in prompt


def test_explorer_prompt_without_repos():
    """Explorer prompt without repos context."""
    prompt = _build_explorer_prompt(
        description="что делает модуль auth?",
        task_id="test-456",
    )
    assert "explorer" in prompt.lower()
    assert "auth" in prompt
    assert "<<<JSON>>>" in prompt


def test_explorer_prompt_json_format():
    """Explorer prompt should request JSON output."""
    prompt = _build_explorer_prompt("test", task_id="abc")
    assert "<<<JSON>>>" in prompt
    assert "<<<END>>>" in prompt
    assert '"status": "done"' in prompt
