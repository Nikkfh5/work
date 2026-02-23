"""
tests/test_config_validator.py — тесты для supervisor/config_validator.py

Запуск: pytest tests/test_config_validator.py -v
"""

import os
import pytest
from supervisor.config_validator import validate_agents_yaml


# ── Вспомогательные фикстуры ────────────────────────────────────────────────

def make_config(workers: dict = None, supervisor: dict = None) -> dict:
    cfg = {}
    if workers is not None:
        cfg["workers"] = workers
    if supervisor is not None:
        cfg["supervisor"] = supervisor
    return cfg


def minimal_worker(
    active=True,
    is_reviewer=False,
    is_health=False,
    reviewer_id="job1_reviewer",
    token_env=None,
    delivery_mode=None,
    clone_strategy=None,
) -> dict:
    w = {
        "model": "claude-opus-4-6",
        "active": active,
        "is_reviewer": is_reviewer,
        "is_health": is_health,
    }
    if reviewer_id:
        w["reviewer_id"] = reviewer_id
    if token_env:
        w["git_token_env"] = token_env
    if delivery_mode:
        w["delivery_policy"] = {"mode": delivery_mode}
    if clone_strategy:
        w["repos"] = [{"alias": "api", "url": "https://example.com/repo.git",
                       "clone_strategy": clone_strategy}]
    return w


# ── Тесты корректных конфигов ────────────────────────────────────────────────

def test_valid_config_returns_no_errors():
    """Правильный конфиг — пустой список ошибок."""
    config = make_config(workers={
        "job1_worker": minimal_worker(reviewer_id="job1_reviewer"),
        "job1_reviewer": minimal_worker(is_reviewer=True, reviewer_id=None),
        "health_monitor": minimal_worker(is_health=True, reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert errors == []


def test_inactive_worker_no_errors_without_reviewer():
    """Неактивный воркер без reviewer_id — не ошибка."""
    config = make_config(workers={
        "job1_worker": minimal_worker(active=False, reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert errors == []


def test_health_monitor_no_reviewer_required():
    """is_health=true — reviewer_id не нужен."""
    config = make_config(workers={
        "health_monitor": minimal_worker(is_health=True, reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert errors == []


def test_reviewer_no_reviewer_id_required():
    """is_reviewer=true — reviewer_id не нужен."""
    config = make_config(workers={
        "job1_reviewer": minimal_worker(is_reviewer=True, reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert errors == []


def test_empty_workers_no_errors():
    """Нет воркеров — нет ошибок."""
    config = make_config(workers={})
    errors = validate_agents_yaml(config)
    assert errors == []


# ── Тесты ошибочных конфигов ─────────────────────────────────────────────────

def test_active_worker_without_reviewer_id_errors():
    """Активный воркер без reviewer_id и без is_reviewer/is_health — ошибка."""
    config = make_config(workers={
        "job1_worker": minimal_worker(reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert any("reviewer_id" in e for e in errors)


def test_reviewer_id_references_nonexistent_worker_errors():
    """reviewer_id указывает на несуществующий воркер — ошибка."""
    config = make_config(workers={
        "job1_worker": minimal_worker(reviewer_id="nonexistent_reviewer"),
    })
    errors = validate_agents_yaml(config)
    assert any("does not exist" in e for e in errors)


def test_invalid_delivery_mode_errors():
    """Невалидный delivery_policy.mode — ошибка."""
    config = make_config(workers={
        "job1_worker": minimal_worker(reviewer_id="rev", delivery_mode="ftp"),
        "rev": minimal_worker(is_reviewer=True, reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert any("delivery_policy.mode" in e for e in errors)


def test_valid_delivery_modes():
    """Все валидные delivery_policy.mode — нет ошибок."""
    for mode in ("push", "pr", "patch_to_owner"):
        config = make_config(workers={
            "job1_worker": minimal_worker(reviewer_id="rev", delivery_mode=mode),
            "rev": minimal_worker(is_reviewer=True, reviewer_id=None),
        })
        errors = validate_agents_yaml(config)
        assert not any("delivery_policy.mode" in e for e in errors), \
            f"mode={mode!r} should be valid"


def test_invalid_clone_strategy_errors():
    """Невалидная clone_strategy — ошибка."""
    config = make_config(workers={
        "job1_worker": minimal_worker(reviewer_id="rev", clone_strategy="blobless"),
        "rev": minimal_worker(is_reviewer=True, reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert any("clone_strategy" in e for e in errors)


def test_valid_clone_strategies():
    """Все валидные clone_strategy — нет ошибок."""
    for strategy in ("mirror", "shallow", "partial", "sparse"):
        config = make_config(workers={
            "job1_worker": minimal_worker(reviewer_id="rev", clone_strategy=strategy),
            "rev": minimal_worker(is_reviewer=True, reviewer_id=None),
        })
        errors = validate_agents_yaml(config)
        assert not any("clone_strategy" in e for e in errors), \
            f"strategy={strategy!r} should be valid"


def test_missing_git_token_env_errors(monkeypatch):
    """git_token_env указан, но переменной нет в окружении — ошибка."""
    monkeypatch.delenv("GIT_TOKEN_MISSING", raising=False)
    config = make_config(workers={
        "job1_worker": minimal_worker(reviewer_id="rev", token_env="GIT_TOKEN_MISSING"),
        "rev": minimal_worker(is_reviewer=True, reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert any("GIT_TOKEN_MISSING" in e for e in errors)


def test_present_git_token_env_no_errors(monkeypatch):
    """git_token_env есть в окружении — нет ошибки."""
    monkeypatch.setenv("GIT_TOKEN_PRESENT", "ghp_fake_token")
    config = make_config(workers={
        "job1_worker": minimal_worker(reviewer_id="rev", token_env="GIT_TOKEN_PRESENT"),
        "rev": minimal_worker(is_reviewer=True, reviewer_id=None),
    })
    errors = validate_agents_yaml(config)
    assert not any("GIT_TOKEN_PRESENT" in e for e in errors)


def test_non_dict_root_returns_error():
    """Если root не dict — немедленная ошибка."""
    errors = validate_agents_yaml("not a dict")
    assert errors
    assert "root element" in errors[0]
