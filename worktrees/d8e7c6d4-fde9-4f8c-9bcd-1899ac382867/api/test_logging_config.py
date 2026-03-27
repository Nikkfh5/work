"""Tests for logging_config module."""

import json
import logging
import io
import threading
from datetime import datetime

import structlog
import pytest

from logging_config import setup_logging, get_logger, bind_context, clear_context


@pytest.fixture(autouse=True)
def _reset_logging():
    """Reset logging and structlog state between tests."""
    yield
    logging.getLogger().handlers.clear()
    clear_context()
    structlog.reset_defaults()


def _capture_log_output(logger, level, message, **kwargs):
    """Helper: capture log output as a string."""
    stream = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    # Re-use the formatter from the first handler (set by setup_logging)
    if root.handlers:
        handler.setFormatter(root.handlers[0].formatter)
    root.addHandler(handler)
    try:
        getattr(logger, level)(message, **kwargs)
        return stream.getvalue()
    finally:
        root.removeHandler(handler)


class TestJSONOutput:
    """Verify JSON output contains all required fields."""

    def test_json_contains_timestamp(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("test.timestamp")
        output = _capture_log_output(log, "info", "hello")
        data = json.loads(output)
        assert "timestamp" in data

    def test_json_contains_level(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("test.level")
        output = _capture_log_output(log, "warning", "warn msg")
        data = json.loads(output)
        assert data["log_level"] == "warning"

    def test_json_contains_logger_name(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("myapp.service")
        output = _capture_log_output(log, "info", "test")
        data = json.loads(output)
        assert data["logger_name"] == "myapp.service"

    def test_json_contains_event_message(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("test.event")
        output = _capture_log_output(log, "info", "important event")
        data = json.loads(output)
        assert data["event"] == "important event"

    def test_json_contains_all_fields(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("test.all")
        output = _capture_log_output(log, "error", "failure")
        data = json.loads(output)
        assert "timestamp" in data
        assert "log_level" in data
        assert "logger_name" in data
        assert "event" in data

    def test_json_timestamp_is_iso_format(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("test.iso")
        output = _capture_log_output(log, "info", "check time")
        data = json.loads(output)
        ts = data["timestamp"]
        # Validate strict ISO format — fromisoformat raises on invalid strings
        datetime.fromisoformat(ts)


class TestBindContext:
    """Verify bind_context adds variables to log entries."""

    def test_bind_request_id(self):
        setup_logging(level="DEBUG", json_output=True)
        bind_context(request_id="req-123")
        log = get_logger("test.ctx")
        output = _capture_log_output(log, "info", "with context")
        data = json.loads(output)
        assert data["request_id"] == "req-123"

    def test_bind_multiple_fields(self):
        setup_logging(level="DEBUG", json_output=True)
        bind_context(request_id="req-456", user_id="usr-789")
        log = get_logger("test.multi")
        output = _capture_log_output(log, "info", "multi ctx")
        data = json.loads(output)
        assert data["request_id"] == "req-456"
        assert data["user_id"] == "usr-789"

    def test_bind_context_incremental(self):
        setup_logging(level="DEBUG", json_output=True)
        bind_context(request_id="req-001")
        bind_context(user_id="usr-002")
        log = get_logger("test.inc")
        output = _capture_log_output(log, "info", "incremental")
        data = json.loads(output)
        assert data["request_id"] == "req-001"
        assert data["user_id"] == "usr-002"

    def test_clear_context_removes_fields(self):
        setup_logging(level="DEBUG", json_output=True)
        bind_context(request_id="req-xxx")
        clear_context()
        log = get_logger("test.clear")
        output = _capture_log_output(log, "info", "after clear")
        data = json.loads(output)
        assert "request_id" not in data

    def test_thread_isolation(self):
        """bind_context in a child thread must not leak into the main thread."""
        setup_logging(level="DEBUG", json_output=True)
        clear_context()

        def child():
            bind_context(request_id="child-req")

        t = threading.Thread(target=child)
        t.start()
        t.join()

        log = get_logger("test.isolation")
        output = _capture_log_output(log, "info", "main thread")
        data = json.loads(output)
        assert "request_id" not in data


class TestLevelFiltering:
    """Verify log level filtering works correctly."""

    def test_info_level_filters_debug(self):
        setup_logging(level="INFO", json_output=True)
        log = get_logger("test.filter")
        output = _capture_log_output(log, "debug", "should not appear")
        assert output == ""

    def test_info_level_passes_info(self):
        setup_logging(level="INFO", json_output=True)
        log = get_logger("test.pass")
        output = _capture_log_output(log, "info", "should appear")
        assert output != ""
        data = json.loads(output)
        assert data["event"] == "should appear"

    def test_info_level_passes_warning(self):
        setup_logging(level="INFO", json_output=True)
        log = get_logger("test.warn")
        output = _capture_log_output(log, "warning", "warn msg")
        assert output != ""

    def test_error_level_filters_info(self):
        setup_logging(level="ERROR", json_output=True)
        log = get_logger("test.err")
        output = _capture_log_output(log, "info", "filtered out")
        assert output == ""

    def test_error_level_passes_error(self):
        setup_logging(level="ERROR", json_output=True)
        log = get_logger("test.err2")
        output = _capture_log_output(log, "error", "error msg")
        assert output != ""
        data = json.loads(output)
        assert data["event"] == "error msg"

    def test_debug_level_passes_all(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("test.dbg")
        output = _capture_log_output(log, "debug", "debug msg")
        assert output != ""


class TestConsoleOutput:
    """Verify dev (non-JSON) mode works without errors."""

    def test_console_renderer_does_not_crash(self):
        setup_logging(level="DEBUG", json_output=False)
        log = get_logger("test.console")
        output = _capture_log_output(log, "info", "console test")
        # Console output is human-readable, not JSON
        assert "console test" in output

    def test_console_output_is_not_json(self):
        setup_logging(level="DEBUG", json_output=False)
        log = get_logger("test.notjson")
        output = _capture_log_output(log, "info", "not json")
        with pytest.raises(json.JSONDecodeError):
            json.loads(output)


class TestGetLogger:
    """Verify get_logger returns a usable bound logger."""

    def test_returns_bound_logger(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("mymodule")
        assert hasattr(log, "info")
        assert hasattr(log, "warning")
        assert hasattr(log, "error")
        assert hasattr(log, "debug")

    def test_logger_with_extra_kwargs(self):
        setup_logging(level="DEBUG", json_output=True)
        log = get_logger("test.extra")
        output = _capture_log_output(log, "info", "with extra", order_id="ORD-42")
        data = json.loads(output)
        assert data["order_id"] == "ORD-42"
        assert data["event"] == "with extra"
