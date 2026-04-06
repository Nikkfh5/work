"""
supervisor/log_utils.py — утилиты для безопасного логирования.

Функция redact() удаляет секреты (токены, пароли, URL с кредами)
из строк перед записью в лог-файлы или передачей в DB.

Инвариант: redact() вызывается ДО любой записи в файл или БД.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Порядок важен: более специфичные паттерны — первыми
REDACT_PATTERNS: list[tuple[str, str]] = [
    # HTTPS URL с кредами вида https://user:password@host
    (r"https://[^:@\s]+:[^@\s]+@", "https://[REDACTED]@"),
    # GitHub personal access token (ghp_, gho_, ghu_, ghs_, ghr_)
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "[GITHUB_TOKEN]"),
    # GitLab personal access token
    (r"glpat-[A-Za-z0-9_-]{20,}", "[GITLAB_TOKEN]"),
    # Notion integration token
    (r"secret_[A-Za-z0-9]{40,}", "[NOTION_TOKEN]"),
    # Telegram Bot token: full format bot_id:AAtoken (more specific — first)
    (r"\d{5,}:AA[A-Za-z0-9_-]{20,}", "[TG_BOT_TOKEN]"),
    # Telegram Bot token: partial (AAF... fragment)
    (r"AAF[A-Za-z0-9_-]{30,}", "[TG_TOKEN]"),
    # Anthropic API key
    (r"sk-ant-api[A-Za-z0-9_-]{20,}", "[ANTHROPIC_KEY]"),
    # Generic bearer token в заголовках
    (r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}", "Bearer [REDACTED]"),
]

_compiled: list[tuple[re.Pattern, str]] = [
    (re.compile(pattern), replacement) for pattern, replacement in REDACT_PATTERNS
]


def redact(text: str) -> str:
    """
    Удалить секреты из строки.

    Применяет все REDACT_PATTERNS последовательно.
    Никогда не бросает исключений — при ошибке возвращает текст без изменений.

    Args:
        text: входная строка, возможно содержащая секреты

    Returns:
        Строка с заменёнными секретами
    """
    if not text:
        return text
    try:
        result = text
        for pattern, replacement in _compiled:
            result = pattern.sub(replacement, result)
        return result
    except Exception:
        logger.warning(
            "redact() failed — returning original text (possible secret leak)"
        )
        return text
