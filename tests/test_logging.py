"""Tests for structured JSON logging and request ID correlation."""

from __future__ import annotations

import json
import logging

from creel.log import (
    generate_request_id,
    request_id_var,
    setup_logging,
)


class TestJSONMode:
    """Test JSON logging output."""

    def test_json_mode_outputs_valid_json(self, capfd):
        setup_logging(json_mode=True, level="DEBUG")
        logger = logging.getLogger("test.json")
        logger.info("hello world")

        captured = capfd.readouterr()
        record = json.loads(captured.err.strip())
        assert record["level"] == "INFO"
        assert record["logger"] == "test.json"
        assert record["message"] == "hello world"
        assert "timestamp" in record

    def test_json_mode_includes_extra_fields(self, capfd):
        setup_logging(json_mode=True, level="DEBUG")
        logger = logging.getLogger("test.extra")
        logger.info("with extra", extra={"foo": "bar"})

        captured = capfd.readouterr()
        record = json.loads(captured.err.strip())
        assert record["foo"] == "bar"

    def test_json_mode_includes_request_id(self, capfd):
        setup_logging(json_mode=True, level="DEBUG")
        token = request_id_var.set("test-rid-123")
        try:
            logger = logging.getLogger("test.rid")
            logger.info("with rid")

            captured = capfd.readouterr()
            record = json.loads(captured.err.strip())
            assert record["request_id"] == "test-rid-123"
        finally:
            request_id_var.reset(token)


class TestHumanReadableMode:
    """Test that human-readable mode still works."""

    def test_human_readable_format(self, capfd):
        setup_logging(json_mode=False, level="DEBUG")
        logger = logging.getLogger("test.human")
        logger.info("readable message")

        captured = capfd.readouterr()
        line = captured.err.strip()
        assert "readable message" in line
        assert "[INFO]" in line
        assert "test.human" in line
        # Should NOT be valid JSON
        try:
            json.loads(line)
            assert False, "Should not be valid JSON"
        except json.JSONDecodeError:
            pass


class TestRequestId:
    """Test request ID generation and context propagation."""

    def test_generate_request_id_format(self):
        rid = generate_request_id()
        assert len(rid) == 8
        # Should be hex characters
        int(rid, 16)

    def test_generate_request_id_unique(self):
        ids = {generate_request_id() for _ in range(100)}
        assert len(ids) == 100

    def test_request_id_context_var_default(self):
        # Reset to default
        token = request_id_var.set(None)
        try:
            assert request_id_var.get() is None
        finally:
            request_id_var.reset(token)

    def test_request_id_propagation(self):
        token = request_id_var.set("abc123")
        try:
            assert request_id_var.get() == "abc123"
        finally:
            request_id_var.reset(token)
