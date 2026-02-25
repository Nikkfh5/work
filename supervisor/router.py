"""
supervisor/router.py — жёсткая привязка контакт → воркер.

Каждый воркер закреплён за конкретным telegram_contact и email.
Супервайзор не ищет свободного — он точно знает кому отдать задачу.
"""

import logging
import yaml
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "agents.yaml"


class Router:
    def __init__(self, config_path: Optional[Path] = None):
        """Загрузить agents.yaml и построить индексы контакт → worker_id."""
        path = config_path or CONFIG_PATH
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self._workers: dict[str, dict] = cfg.get("workers", {})
        self._supervisor_cfg: dict = cfg.get("supervisor", {})

        # Индексы для быстрого поиска
        self._by_telegram: dict[str, str] = {}  # @contact → worker_id
        self._by_email: dict[str, str] = {}      # email → worker_id

        for worker_id, worker_cfg in self._workers.items():
            if not worker_cfg.get("active", False):
                continue
            tg = worker_cfg.get("telegram_contact", "").lower()
            email = worker_cfg.get("email", "").lower()
            if tg:
                self._by_telegram[tg] = worker_id
            if email:
                self._by_email[email] = worker_id

        logger.info(
            "Router loaded: %d active workers, %d TG contacts, %d emails",
            len([w for w in self._workers.values() if w.get("active")]),
            len(self._by_telegram),
            len(self._by_email),
        )

    def resolve_worker(self, source: str, contact: str) -> str:
        """
        Определить worker_id по источнику и контакту.

        Args:
            source: 'telegram' или 'email'
            contact: @username или email адрес

        Returns:
            worker_id из agents.yaml

        Raises:
            ValueError: если контакт не привязан ни к одному воркеру
        """
        contact_lower = contact.lower().strip()

        if source == "telegram":
            worker_id = self._by_telegram.get(contact_lower)
        elif source == "email":
            worker_id = self._by_email.get(contact_lower)
        else:
            raise ValueError(f"Unknown source: {source!r}")

        if not worker_id:
            raise ValueError(
                f"No worker assigned for {source} contact {contact!r}. "
                f"Add this contact to config/agents.yaml"
            )

        logger.debug("Resolved %s %s → %s", source, contact, worker_id)
        return worker_id

    def get_worker_config(self, worker_id: str) -> dict:
        """Получить конфиг воркера."""
        if worker_id not in self._workers:
            raise ValueError(f"Unknown worker_id: {worker_id!r}")
        return self._workers[worker_id]

    def get_active_workers(self) -> dict[str, dict]:
        """Все активные воркеры."""
        return {k: v for k, v in self._workers.items() if v.get("active")}

    def get_supervisor_config(self) -> dict:
        """Вернуть секцию supervisor из agents.yaml."""
        return self._supervisor_cfg

    def all_worker_ids(self) -> list[str]:
        """Вернуть список всех worker_id (включая неактивные)."""
        return list(self._workers.keys())
