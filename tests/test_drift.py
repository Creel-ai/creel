"""Tests for the drift detection module."""

from __future__ import annotations

import json
from pathlib import Path

from guardian.drift import DriftDetector


class TestDriftDetectorNewTool:
    """Tests for new tool detection."""

    def test_new_tool_triggers_alert(self) -> None:
        """A tool never seen before should trigger a critical alert."""
        detector = DriftDetector()
        alert = detector.check_tool_call("never_seen_tool")
        assert alert is not None
        assert alert.alert_type == "new_tool"
        assert alert.severity == "critical"
        assert "never_seen_tool" in alert.detail

    def test_known_tool_no_alert(self) -> None:
        """A tool in the baseline should not trigger an alert."""
        detector = DriftDetector()
        detector._baseline.known_tools = {"check_weather", "check_email"}
        alert = detector.check_tool_call("check_weather")
        assert alert is None

    def test_new_tool_grace_count(self) -> None:
        """Grace count should allow N calls before suppressing alert."""
        detector = DriftDetector(new_tool_grace_count=2)
        # First 3 calls (0, 1, 2) should all alert
        alert1 = detector.check_tool_call("new_tool")
        assert alert1 is not None
        alert2 = detector.check_tool_call("new_tool")
        assert alert2 is not None
        alert3 = detector.check_tool_call("new_tool")
        assert alert3 is not None
        # 4th call — tool is now known
        alert4 = detector.check_tool_call("new_tool")
        assert alert4 is None

    def test_zero_grace_adds_after_first_alert(self) -> None:
        """With grace_count=0, tool is added to known set after first alert."""
        detector = DriftDetector(new_tool_grace_count=0)
        alert1 = detector.check_tool_call("new_tool")
        assert alert1 is not None
        # Second call should not alert — tool is now known
        alert2 = detector.check_tool_call("new_tool")
        assert alert2 is None


class TestDriftDetectorOutputLength:
    """Tests for output length anomaly detection."""

    def test_no_alert_with_insufficient_data(self) -> None:
        """Should not alert until there are at least 10 data points."""
        detector = DriftDetector(z_threshold=3.0)
        # Add 9 normal data points
        for _ in range(9):
            alert = detector.check_output_length("tool", 100)
            assert alert is None

    def test_normal_output_no_alert(self) -> None:
        """Normal output length should not trigger an alert."""
        detector = DriftDetector(z_threshold=3.0)
        # Build a baseline with consistent lengths
        for _ in range(20):
            detector._baseline.output_lengths.append(100)
        detector._recompute_stats()

        alert = detector.check_output_length("tool", 105)
        assert alert is None

    def test_anomalous_output_triggers_alert(self) -> None:
        """Extremely long output should trigger an alert."""
        detector = DriftDetector(z_threshold=3.0)
        # Build a baseline with consistent lengths (~100 +/- 5)
        for i in range(50):
            detector._baseline.output_lengths.append(100 + (i % 10) - 5)
        detector._recompute_stats()

        # A massive spike should exceed z_threshold
        alert = detector.check_output_length("tool", 10000)
        assert alert is not None
        assert alert.alert_type == "response_length_anomaly"
        assert "z-score" in alert.detail


class TestDriftDetectorErrorRate:
    """Tests for error rate spike detection."""

    def test_no_alert_below_threshold(self) -> None:
        """Error rate below threshold should not alert."""
        detector = DriftDetector(error_threshold=0.10)
        # 95 successes, 5 errors = 5% error rate
        for _ in range(95):
            detector.check_error_rate(True)
        for _ in range(5):
            alert = detector.check_error_rate(False)
        assert alert is None

    def test_alert_above_threshold(self) -> None:
        """Error rate above threshold should trigger alert."""
        detector = DriftDetector(error_threshold=0.10, error_window_size=20)
        # 15 successes, then 5 errors = 25% error rate
        for _ in range(15):
            detector.check_error_rate(True)
        alert = None
        for _ in range(5):
            alert = detector.check_error_rate(False)
        assert alert is not None
        assert alert.alert_type == "error_rate_spike"

    def test_no_alert_with_insufficient_data(self) -> None:
        """Should not alert with fewer than 10 data points."""
        detector = DriftDetector(error_threshold=0.01)
        # 9 errors in a row — but below minimum sample size
        for _ in range(9):
            alert = detector.check_error_rate(False)
        assert alert is None

    def test_sliding_window(self) -> None:
        """Error rate should be computed over the sliding window only."""
        detector = DriftDetector(error_threshold=0.10, error_window_size=20)
        # Fill window with errors
        for _ in range(20):
            detector.check_error_rate(False)
        # Now fill with successes — error rate should drop
        for _ in range(20):
            alert = detector.check_error_rate(True)
        # Window is now all successes
        assert alert is None


class TestDriftDetectorCheckAll:
    """Tests for the check_all convenience method."""

    def test_check_all_returns_multiple_alerts(self) -> None:
        """check_all should return alerts from all three checks."""
        detector = DriftDetector(z_threshold=3.0, error_threshold=0.01, error_window_size=20)
        # Build a baseline that will trigger both new tool and error rate
        for _ in range(50):
            detector._baseline.output_lengths.append(100)
        detector._recompute_stats()
        for _ in range(20):
            detector.check_error_rate(False)

        alerts = detector.check_all("brand_new_tool", 100, False)
        # Should get at least new_tool + error_rate
        alert_types = {a.alert_type for a in alerts}
        assert "new_tool" in alert_types
        assert "error_rate_spike" in alert_types

    def test_check_all_no_alerts(self) -> None:
        """check_all should return empty list when everything is normal."""
        detector = DriftDetector()
        detector._baseline.known_tools = {"check_weather"}
        alerts = detector.check_all("check_weather", 100, True)
        assert alerts == []


class TestDriftDetectorBaseline:
    """Tests for baseline building from audit logs."""

    def test_build_baseline_from_audit_log(self, tmp_path: Path) -> None:
        """Should build baseline from an existing audit log file."""
        log_path = tmp_path / "audit.jsonl"
        entries = [
            {"event": "validate_action", "tool_name": "check_weather"},
            {"event": "validate_action", "tool_name": "check_email"},
            {"event": "validate_action", "tool_name": "check_weather"},
            {
                "event": "tool_result",
                "tool_name": "check_weather",
                "output_length": 100,
                "success": True,
            },
            {
                "event": "tool_result",
                "tool_name": "check_email",
                "output_length": 200,
                "success": True,
            },
            {
                "event": "tool_result",
                "tool_name": "check_weather",
                "output_length": 150,
                "success": False,
            },
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        detector = DriftDetector(audit_log_path=log_path)

        assert "check_weather" in detector._baseline.known_tools
        assert "check_email" in detector._baseline.known_tools
        assert len(detector._baseline.output_lengths) == 3
        assert len(detector._baseline.recent_results) == 3

    def test_build_baseline_missing_file(self, tmp_path: Path) -> None:
        """Missing audit log should result in empty baseline."""
        detector = DriftDetector(audit_log_path=tmp_path / "nonexistent.jsonl")
        assert detector._baseline.known_tools == set()
        assert len(detector._baseline.output_lengths) == 0

    def test_build_baseline_empty_file(self, tmp_path: Path) -> None:
        """Empty audit log should result in empty baseline."""
        log_path = tmp_path / "audit.jsonl"
        log_path.write_text("")
        detector = DriftDetector(audit_log_path=log_path)
        assert detector._baseline.known_tools == set()

    def test_build_baseline_corrupt_lines(self, tmp_path: Path) -> None:
        """Corrupt lines in audit log should be skipped."""
        log_path = tmp_path / "audit.jsonl"
        log_path.write_text(
            'not json\n{"event": "validate_action", "tool_name": "check_weather"}\n{bad json}\n'
        )
        detector = DriftDetector(audit_log_path=log_path)
        assert "check_weather" in detector._baseline.known_tools
