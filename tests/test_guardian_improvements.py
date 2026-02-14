"""Tests for guardian improvements: warm-up, latency, conditional judge, audit enhancements."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from guardian.audit import AuditLogger, read_audit_log
from guardian.fast_classifier import FastClassifier
from guardian.llm_judge import LLMJudge
from guardian.types import (
    AuditConfig,
    FastClassifierConfig,
    LLMJudgeConfig,
)


# --- Fast Classifier ---


class TestFastClassifierWarmUp:
    def test_warm_up_disabled(self) -> None:
        """Warm-up should be a no-op when disabled."""
        config = FastClassifierConfig(enabled=False)
        classifier = FastClassifier(config)
        classifier.warm_up()  # should not raise
        assert classifier.backend == "none"

    def test_warm_up_unavailable(self) -> None:
        """Warm-up should handle unavailable model gracefully.

        With a nonexistent model, _load() will raise RuntimeError (either
        because backends aren't installed, or because model download fails).
        The constructor doesn't call _load(); warm_up() only runs inference
        if a pipeline was already loaded, so it's a no-op here.
        """
        config = FastClassifierConfig(enabled=True, model_name="nonexistent/model-that-does-not-exist-anywhere")
        # _load() is called lazily or not at all — the constructor just stores config.
        # If _load() is called eagerly and raises, that's expected for a bad model.
        try:
            classifier = FastClassifier(config)
        except RuntimeError:
            return  # acceptable: bad model can't load
        classifier.warm_up()  # should not raise
        assert classifier.backend == "none"

    def test_backend_property_default(self) -> None:
        """Backend is 'none' when classifier is disabled (no load attempt)."""
        config = FastClassifierConfig(enabled=False)
        classifier = FastClassifier(config)
        assert classifier.backend == "none"


# --- LLM Judge ---


class TestLLMJudgeConditional:
    def test_should_run_disabled(self) -> None:
        config = LLMJudgeConfig(enabled=False)
        judge = LLMJudge(config)
        assert judge.should_run(0.7) is False

    def test_should_run_uncertain_only_in_range(self) -> None:
        config = LLMJudgeConfig(
            enabled=True, uncertain_only=True,
            uncertain_low=0.5, uncertain_high=0.85,
        )
        judge = LLMJudge(config)
        assert judge.should_run(0.7) is True

    def test_should_run_uncertain_only_below_range(self) -> None:
        config = LLMJudgeConfig(
            enabled=True, uncertain_only=True,
            uncertain_low=0.5, uncertain_high=0.85,
        )
        judge = LLMJudge(config)
        assert judge.should_run(0.3) is False

    def test_should_run_uncertain_only_above_range(self) -> None:
        config = LLMJudgeConfig(
            enabled=True, uncertain_only=True,
            uncertain_low=0.5, uncertain_high=0.85,
        )
        judge = LLMJudge(config)
        assert judge.should_run(0.95) is False

    def test_should_run_no_classifier_confidence(self) -> None:
        """Without classifier confidence, always run if enabled."""
        config = LLMJudgeConfig(enabled=True, uncertain_only=True)
        judge = LLMJudge(config)
        assert judge.should_run(None) is True

    def test_should_run_uncertain_only_false(self) -> None:
        """When uncertain_only is off, always run if enabled."""
        config = LLMJudgeConfig(enabled=True, uncertain_only=False)
        judge = LLMJudge(config)
        assert judge.should_run(0.1) is True
        assert judge.should_run(0.99) is True

    def test_usage_stats_initial(self) -> None:
        config = LLMJudgeConfig(enabled=True)
        judge = LLMJudge(config)
        stats = judge.usage_stats
        assert stats["calls"] == 0
        assert stats["input_tokens"] == 0
        assert stats["output_tokens"] == 0


# --- Audit Logger ---


class TestAuditLogToolResult:
    @pytest.fixture
    def log_file(self, tmp_path: Path) -> Path:
        return tmp_path / "audit.jsonl"

    @pytest.fixture
    def audit(self, log_file: Path) -> AuditLogger:
        return AuditLogger(log_file)

    def test_log_tool_result_success(self, audit: AuditLogger, log_file: Path) -> None:
        audit.log_tool_result(
            tool_name="check_weather",
            success=True,
            duration_ms=123.4,
            output_length=500,
        )
        lines = log_file.read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["event"] == "tool_result"
        assert record["tool_name"] == "check_weather"
        assert record["success"] is True
        assert record["duration_ms"] == 123.4
        assert record["output_length"] == 500
        assert "error" not in record

    def test_log_tool_result_failure(self, audit: AuditLogger, log_file: Path) -> None:
        audit.log_tool_result(
            tool_name="send_email",
            success=False,
            duration_ms=50.0,
            output_length=0,
            error="Connection refused",
        )
        record = json.loads(log_file.read_text().strip())
        assert record["success"] is False
        assert record["error"] == "Connection refused"

    def test_error_truncated(self, audit: AuditLogger, log_file: Path) -> None:
        long_error = "x" * 500
        audit.log_tool_result(
            tool_name="test",
            success=False,
            duration_ms=1.0,
            output_length=0,
            error=long_error,
        )
        record = json.loads(log_file.read_text().strip())
        assert len(record["error"]) == 200


class TestAuditLogRotation:
    def test_daily_rotation_creates_dated_file(self, tmp_path: Path) -> None:
        audit = AuditLogger(tmp_path / "audit.jsonl", rotate_daily=True)
        audit.log_screen(
            input_hash="x", input_length=1, blocked=False, source="test"
        )
        # Should create a dated file, not plain audit.jsonl
        files = list(tmp_path.glob("audit-*.jsonl"))
        assert len(files) == 1
        assert not (tmp_path / "audit.jsonl").exists()

    def test_size_rotation(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        # Create a small max size
        audit = AuditLogger(log_file, max_size_mb=0.001)  # ~1KB
        # Write enough to trigger rotation
        for i in range(50):
            audit.log_screen(
                input_hash=f"hash{i}", input_length=i, blocked=False, source="test"
            )
        # Should have rotated
        assert log_file.exists()
        rotated = log_file.with_suffix(".jsonl.1")
        assert rotated.exists()


class TestReadAuditLog:
    @pytest.fixture
    def log_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "audit.jsonl"
        entries = [
            {"event": "screen_input", "blocked": False, "ts": "2026-01-01"},
            {"event": "screen_input", "blocked": True, "ts": "2026-01-02"},
            {"event": "validate_action", "verdict": "allow", "ts": "2026-01-03"},
            {"event": "validate_action", "verdict": "deny", "ts": "2026-01-04"},
            {"event": "tool_result", "success": True, "ts": "2026-01-05"},
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return path

    def test_read_all(self, log_file: Path) -> None:
        entries = read_audit_log(log_file)
        assert len(entries) == 5

    def test_tail(self, log_file: Path) -> None:
        entries = read_audit_log(log_file, tail=2)
        assert len(entries) == 2

    def test_event_filter(self, log_file: Path) -> None:
        entries = read_audit_log(log_file, event_filter="tool_result")
        assert len(entries) == 1

    def test_blocked_only(self, log_file: Path) -> None:
        entries = read_audit_log(log_file, blocked_only=True)
        assert len(entries) == 1
        assert entries[0]["blocked"] is True

    def test_denied_only(self, log_file: Path) -> None:
        entries = read_audit_log(log_file, denied_only=True)
        assert len(entries) == 1
        assert entries[0]["verdict"] == "deny"

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        entries = read_audit_log(tmp_path / "nope.jsonl")
        assert entries == []
