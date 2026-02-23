"""
supervisor/config_validator.py — fail-fast валидация agents.yaml на старте.

Проверяет структурную корректность конфига перед запуском системы.
При ошибках — logger.critical + sys.exit(1).

Использование:
    from supervisor.config_validator import validate_agents_yaml, load_and_validate

    errors = validate_agents_yaml(config_dict)
    if errors:
        for e in errors:
            logger.critical(e)
        sys.exit(1)
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

VALID_DELIVERY_MODES = {"push", "pr", "patch_to_owner"}
VALID_CLONE_STRATEGIES = {"mirror", "shallow", "partial", "sparse"}


def validate_agents_yaml(config: dict) -> list[str]:
    """
    Валидировать содержимое agents.yaml.

    Args:
        config: распарсенный словарь из agents.yaml

    Returns:
        Список строк с ошибками. Пустой список — конфиг валиден.
    """
    errors: list[str] = []

    if not isinstance(config, dict):
        return ["agents.yaml: root element must be a mapping"]

    workers: dict = config.get("workers", {})
    if not isinstance(workers, dict):
        errors.append("agents.yaml: 'workers' must be a mapping")
        return errors

    # Собираем все worker_id для проверки reviewer_id ссылок
    all_worker_ids = set(workers.keys())

    for worker_id, worker_cfg in workers.items():
        if not isinstance(worker_cfg, dict):
            errors.append(f"workers.{worker_id}: must be a mapping")
            continue

        if not worker_cfg.get("active", False):
            continue  # Неактивные воркеры не проверяем строго

        is_reviewer = worker_cfg.get("is_reviewer", False)
        is_health = worker_cfg.get("is_health", False)

        # Обычный worker должен иметь reviewer_id
        if not is_reviewer and not is_health:
            reviewer_id = worker_cfg.get("reviewer_id")
            if not reviewer_id:
                errors.append(
                    f"workers.{worker_id}: active worker must have 'reviewer_id' "
                    f"(or set is_reviewer/is_health=true)"
                )
            elif reviewer_id not in all_worker_ids:
                errors.append(
                    f"workers.{worker_id}: reviewer_id={reviewer_id!r} "
                    f"does not exist in workers"
                )

        # Проверяем git_token_env если задан
        token_env = worker_cfg.get("git_token_env")
        if token_env and token_env not in os.environ:
            errors.append(
                f"workers.{worker_id}: git_token_env={token_env!r} "
                f"is not set in environment"
            )

        # Проверяем repos если есть
        repos = worker_cfg.get("repos", [])
        if isinstance(repos, list):
            for i, repo in enumerate(repos):
                if not isinstance(repo, dict):
                    errors.append(f"workers.{worker_id}.repos[{i}]: must be a mapping")
                    continue
                # clone_strategy
                strategy = repo.get("clone_strategy", "mirror")
                if strategy not in VALID_CLONE_STRATEGIES:
                    errors.append(
                        f"workers.{worker_id}.repos[{i}].clone_strategy={strategy!r} "
                        f"is invalid. Valid: {sorted(VALID_CLONE_STRATEGIES)}"
                    )
                # repo token_env
                repo_token = repo.get("token_env")
                if repo_token and repo_token not in os.environ:
                    errors.append(
                        f"workers.{worker_id}.repos[{i}].token_env={repo_token!r} "
                        f"is not set in environment"
                    )

        # Проверяем delivery_policy
        delivery_policy = worker_cfg.get("delivery_policy", {})
        if isinstance(delivery_policy, dict):
            mode = delivery_policy.get("mode")
            if mode is not None and mode not in VALID_DELIVERY_MODES:
                errors.append(
                    f"workers.{worker_id}.delivery_policy.mode={mode!r} "
                    f"is invalid. Valid: {sorted(VALID_DELIVERY_MODES)}"
                )

    return errors


def load_and_validate(
    config_path: Optional[Path] = None,
    exit_on_error: bool = True,
) -> dict:
    """
    Загрузить agents.yaml и валидировать.

    Args:
        config_path: путь к файлу (по умолчанию config/agents.yaml рядом с проектом)
        exit_on_error: если True — sys.exit(1) при ошибках валидации

    Returns:
        Распарсенный config dict

    Raises:
        FileNotFoundError: если файл не найден
        yaml.YAMLError: если YAML невалиден
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config" / "agents.yaml"

    logger.info("Loading agents.yaml from %s", config_path)
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    errors = validate_agents_yaml(config)

    if errors:
        for error in errors:
            logger.critical("Config error: %s", error)
        if exit_on_error:
            logger.critical("agents.yaml validation failed — stopping")
            sys.exit(1)

    logger.info("agents.yaml validated OK")
    return config
