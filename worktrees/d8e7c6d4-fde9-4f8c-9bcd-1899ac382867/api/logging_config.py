"""Structured logging configuration using structlog.

Provides setup_logging(), get_logger(), and bind_context() for
consistent JSON or colored console output with contextual fields.
"""

import logging
import threading
from typing import Any

import structlog

_thread_local = threading.local()


def _inject_thread_context(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor that merges thread-local context into every log entry."""
    ctx = getattr(_thread_local, "context", None)
    if ctx:
        event_dict.update(ctx)
    return event_dict


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog with shared processors.

    Args:
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_output: If True, render as JSON (production). Otherwise colored console output (dev).
    """
    shared_processors: list[structlog.types.Processor] = [
        _inject_thread_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root_logger = logging.getLogger()
    # Remove existing handlers to avoid duplicates on repeated calls
    root_logger.handlers.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger with the given name.

    Args:
        name: Logger name (typically __name__).

    Returns:
        A structlog BoundLogger instance.
    """
    return structlog.get_logger(name)


def bind_context(**kwargs: Any) -> None:
    """Add contextual key-value pairs to all subsequent logs in the current thread.

    Args:
        **kwargs: Context variables (e.g. request_id, user_id).
    """
    ctx = getattr(_thread_local, "context", None)
    if ctx is None:
        ctx = {}
        _thread_local.context = ctx
    ctx.update(kwargs)


def clear_context() -> None:
    """Remove all thread-local context variables."""
    _thread_local.context = {}
