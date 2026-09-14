"""Unit tests for structured logging and trace correlation."""

import json
import logging
from unittest.mock import MagicMock, patch

from opentelemetry.trace import SpanContext, TraceFlags

from src.telemetry import StructuredJsonFormatter


def test_structured_json_formatter_standard():
    formatter = StructuredJsonFormatter(project_id="test-project")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=42,
        msg="Test info message",
        args=(),
        exc_info=None,
    )

    formatted_str = formatter.format(record)
    parsed = json.loads(formatted_str)

    assert parsed["message"] == "Test info message"
    assert parsed["severity"] == "INFO"
    assert parsed["logger"] == "test_logger"
    assert "logging.googleapis.com/trace" not in parsed


def test_structured_json_formatter_with_trace():
    formatter = StructuredJsonFormatter(project_id="test-project")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.ERROR,
        pathname="test.py",
        lineno=100,
        msg="Test error with trace context",
        args=(),
        exc_info=None,
    )

    mock_span = MagicMock()
    mock_span.get_span_context.return_value = SpanContext(
        trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736,
        span_id=0x00F067AA0BA902B7,
        is_remote=False,
        trace_flags=TraceFlags(0x01),
    )

    with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
        formatted_str = formatter.format(record)
        parsed = json.loads(formatted_str)

        assert parsed["message"] == "Test error with trace context"
        assert parsed["severity"] == "ERROR"
        assert (
            parsed["logging.googleapis.com/trace"]
            == "projects/test-project/traces/4bf92f3577b34da6a3ce929d0e0e4736"
        )
        assert parsed["logging.googleapis.com/spanId"] == "00f067aa0ba902b7"
        assert parsed["logging.googleapis.com/trace_sampled"] is True
