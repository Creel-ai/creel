"""Tests for the audit CLI command and read_audit_log filters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guardian.audit import AuditLogger, read_audit_log


@pytest.fixture
def audit_file(tmp_path: Path) -> Path:
    """Create an audit log with sample entries."""
    log_file = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_file)

    # Screen events
    logger.log_screen(input_hash="h1", input_length=10, blocked=False, source="fast_classifier", confidence=0.1)
    logger.log_screen(input_hash="h2", input_length=20, blocked=True, source="fast_classifier", confidence=0.95)
    logger.log_screen(input_hash="h3", input_length=15, blocked=False, source="fast_classifier", confidence=0.3)

    # Action events
    logger.log_action(tool_name="send_email", arg_keys=["to", "body"], verdict="review", matched_rule="send_*")
    logger.log_action(tool_name="check_weather", arg_keys=["location"], verdict="allow", matched_rule="check_weather")
    logger.log_action(tool_name="delete_file", arg_keys=["path"], verdict="deny", matched_rule="delete_*")

    # Tool result
    logger.log_tool_result(tool_name="send_email", success=True, duration_ms=150.0, output_length=42)

    return log_file


class TestReadAuditLog:
    def test_read_all(self, audit_file: Path) -> None:
        entries = read_audit_log(audit_file)
        assert len(entries) == 7

    def test_tail(self, audit_file: Path) -> None:
        entries = read_audit_log(audit_file, tail=3)
        assert len(entries) == 3

    def test_blocked_only(self, audit_file: Path) -> None:
        entries = read_audit_log(audit_file, blocked_only=True)
        assert len(entries) == 1
        assert entries[0]["blocked"] is True

    def test_denied_only(self, audit_file: Path) -> None:
        entries = read_audit_log(audit_file, denied_only=True)
        assert len(entries) == 1
        assert entries[0]["verdict"] == "deny"

    def test_event_filter(self, audit_file: Path) -> None:
        entries = read_audit_log(audit_file, event_filter="screen_input")
        assert len(entries) == 3
        assert all(e["event"] == "screen_input" for e in entries)

    def test_tool_filter(self, audit_file: Path) -> None:
        entries = read_audit_log(audit_file, tool_filter="send_email")
        assert len(entries) == 2  # validate_action + tool_result
        assert all(e["tool_name"] == "send_email" for e in entries)

    def test_tool_filter_no_match(self, audit_file: Path) -> None:
        entries = read_audit_log(audit_file, tool_filter="nonexistent_tool")
        assert len(entries) == 0

    def test_since_filter(self, audit_file: Path) -> None:
        # All entries have today's timestamp, so filtering with a past date should return all
        entries = read_audit_log(audit_file, since="2020-01-01")
        assert len(entries) == 7

    def test_since_filter_future(self, audit_file: Path) -> None:
        entries = read_audit_log(audit_file, since="2099-01-01")
        assert len(entries) == 0

    def test_combined_filters(self, audit_file: Path) -> None:
        entries = read_audit_log(
            audit_file,
            event_filter="validate_action",
            tool_filter="send_email",
        )
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "send_email"
        assert entries[0]["event"] == "validate_action"

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        entries = read_audit_log(tmp_path / "nope.jsonl")
        assert entries == []

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        entries = read_audit_log(f)
        assert entries == []

    def test_malformed_json_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.jsonl"
        f.write_text('{"event":"good"}\nnot json\n{"event":"also_good"}\n')
        entries = read_audit_log(f)
        assert len(entries) == 2
