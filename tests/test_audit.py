"""Tests for the audit logger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guardian.audit import AuditLogger, _hash_text


class TestHashText:
    def test_deterministic(self) -> None:
        assert _hash_text("hello") == _hash_text("hello")

    def test_different_inputs_different_hashes(self) -> None:
        assert _hash_text("hello") != _hash_text("world")

    def test_truncated_to_16_chars(self) -> None:
        assert len(_hash_text("test input")) == 16

    def test_hex_characters(self) -> None:
        h = _hash_text("abc")
        assert all(c in "0123456789abcdef" for c in h)


class TestAuditLogger:
    @pytest.fixture
    def log_file(self, tmp_path: Path) -> Path:
        return tmp_path / "audit.jsonl"

    @pytest.fixture
    def logger(self, log_file: Path) -> AuditLogger:
        return AuditLogger(log_file)

    def test_log_screen_creates_file(self, logger: AuditLogger, log_file: Path) -> None:
        logger.log_screen(
            input_hash="abc123",
            input_length=42,
            blocked=False,
            source="fast_classifier",
            confidence=0.3,
        )
        assert log_file.exists()

    def test_log_screen_writes_jsonl(self, logger: AuditLogger, log_file: Path) -> None:
        logger.log_screen(
            input_hash="abc123",
            input_length=42,
            blocked=True,
            source="fast_classifier",
            confidence=0.95,
        )
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "screen_input"
        assert record["input_hash"] == "abc123"
        assert record["input_length"] == 42
        assert record["blocked"] is True
        assert record["source"] == "fast_classifier"
        assert record["confidence"] == 0.95
        assert "ts" in record

    def test_log_action_writes_jsonl(self, logger: AuditLogger, log_file: Path) -> None:
        logger.log_action(
            tool_name="send_email",
            arg_keys=["to", "subject", "body"],
            verdict="review",
            matched_rule="send_*",
        )
        lines = log_file.read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["event"] == "validate_action"
        assert record["tool_name"] == "send_email"
        assert record["arg_keys"] == ["to", "subject", "body"]
        assert record["verdict"] == "review"
        assert record["matched_rule"] == "send_*"

    def test_multiple_entries_append(self, logger: AuditLogger, log_file: Path) -> None:
        logger.log_screen(input_hash="a", input_length=1, blocked=False, source="test")
        logger.log_screen(input_hash="b", input_length=2, blocked=True, source="test")
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_no_raw_input_stored(self, logger: AuditLogger, log_file: Path) -> None:
        """Verify the audit log never contains raw input text."""
        logger.log_screen(
            input_hash=_hash_text("secret message"),
            input_length=14,
            blocked=False,
            source="test",
        )
        content = log_file.read_text()
        assert "secret message" not in content

    def test_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        """Write to an invalid path should warn, not crash."""
        bad_logger = AuditLogger(tmp_path / "nonexistent_dir" / "audit.jsonl")
        # Should not raise
        bad_logger.log_screen(input_hash="x", input_length=1, blocked=False, source="test")

    def test_log_screen_debug_writes_jsonl(self, logger: AuditLogger, log_file: Path) -> None:
        chunks = [
            {
                "index": 0,
                "length": 137,
                "label": "INJECTION",
                "score": 0.9953,
                "is_injection": True,
            },
        ]
        logger.log_screen_debug(
            text='{"id": "msg_123"}',
            chunks=chunks,
            blocked=True,
            source="fast_classifier",
        )
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "screen_input_debug"
        assert record["text"] == '{"id": "msg_123"}'
        assert record["blocked"] is True
        assert record["source"] == "fast_classifier"
        assert len(record["chunks"]) == 1
        assert record["chunks"][0]["label"] == "INJECTION"
        assert record["chunks"][0]["score"] == 0.9953
        assert record["chunks"][0]["is_injection"] is True
        assert "ts" in record

    def test_log_screen_debug_multiple_chunks(self, logger: AuditLogger, log_file: Path) -> None:
        chunks = [
            {"index": 0, "length": 2048, "label": "SAFE", "score": 0.99, "is_injection": False},
            {"index": 1, "length": 500, "label": "INJECTION", "score": 0.95, "is_injection": True},
        ]
        logger.log_screen_debug(
            text="x" * 2548,
            chunks=chunks,
            blocked=True,
            source="fast_classifier",
        )
        record = json.loads(log_file.read_text().strip())
        assert len(record["chunks"]) == 2
        assert record["chunks"][0]["is_injection"] is False
        assert record["chunks"][1]["is_injection"] is True

    def test_log_action_outcome_writes_jsonl(self, logger: AuditLogger, log_file: Path) -> None:
        logger.log_action_outcome(
            tool_name="trash_email",
            verdict="review",
            outcome="approved",
        )
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "action_outcome"
        assert record["tool_name"] == "trash_email"
        assert record["verdict"] == "review"
        assert record["outcome"] == "approved"
        assert "ts" in record

    def test_log_action_outcome_denied(self, logger: AuditLogger, log_file: Path) -> None:
        logger.log_action_outcome(
            tool_name="trash_email",
            verdict="deny",
            outcome="denied_by_policy",
        )
        record = json.loads(log_file.read_text().strip())
        assert record["outcome"] == "denied_by_policy"
