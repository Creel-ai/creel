"""Drift detection — behavioral anomaly detection from audit log history.

Builds a baseline of agent behavior from audit logs and flags anomalies:
- New tool usage (tool never seen before)
- Response length anomalies (Z-score > threshold)
- Error rate spikes (sliding window)
"""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum number of output length samples retained for stats
_MAX_OUTPUT_SAMPLES = 10_000


class DriftAlertType(StrEnum):
    """Types of drift detection alerts."""

    NEW_TOOL = "new_tool"
    RESPONSE_LENGTH_ANOMALY = "response_length_anomaly"
    ERROR_RATE_SPIKE = "error_rate_spike"


@dataclass
class DriftAlert:
    """A single drift detection alert."""

    alert_type: DriftAlertType
    tool_name: str = ""
    detail: str = ""
    severity: str = "warning"  # "warning" | "critical"
    timestamp: str = ""


@dataclass
class DriftBaseline:
    """Behavioral baseline computed from audit history."""

    known_tools: set[str] = field(default_factory=set)
    output_lengths: deque[int] = field(
        default_factory=lambda: deque(maxlen=_MAX_OUTPUT_SAMPLES)
    )
    output_length_mean: float = 0.0
    output_length_std: float = 0.0
    recent_results: list[bool] = field(
        default_factory=list
    )  # True=success, False=error


class DriftDetector:
    """Detects behavioral drift by comparing current actions against a baseline.

    The baseline is built from audit log history. Alerts are raised when:
    - The agent calls a tool it has never used before (strong injection signal)
    - Tool output length deviates by more than z_threshold standard deviations
    - Error rate in the sliding window exceeds error_threshold
    """

    def __init__(
        self,
        *,
        z_threshold: float = 3.0,
        error_threshold: float = 0.10,
        error_window_size: int = 100,
        new_tool_grace_count: int = 0,
        audit_log_path: str | Path | None = None,
    ) -> None:
        self.z_threshold = z_threshold
        self.error_threshold = error_threshold
        self.error_window_size = error_window_size
        self.new_tool_grace_count = new_tool_grace_count
        self._baseline = DriftBaseline()
        self._tool_call_counts: dict[str, int] = {}

        if audit_log_path is not None:
            self.build_baseline(audit_log_path)

    def build_baseline(self, audit_log_path: str | Path) -> None:
        """Build a behavioral baseline from an existing audit log file.

        Reads the JSONL audit log and extracts:
        - Set of known tools from validate_action events
        - Output length distribution from tool_result events
        - Recent success/error results for error rate baseline
        """
        path = Path(audit_log_path)
        if not path.exists():
            logger.info("No audit log at %s — starting with empty baseline", path)
            return

        known_tools: set[str] = set()
        output_lengths: list[int] = []
        recent_results: list[bool] = []
        tool_counts: dict[str, int] = {}

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event = entry.get("event", "")

                    if event == "validate_action":
                        tool = entry.get("tool_name", "")
                        if tool:
                            known_tools.add(tool)
                            tool_counts[tool] = tool_counts.get(tool, 0) + 1

                    elif event == "tool_result":
                        length = entry.get("output_length", 0)
                        if isinstance(length, (int, float)):
                            output_lengths.append(int(length))
                        success = entry.get("success", True)
                        recent_results.append(bool(success))

        except Exception:
            logger.warning("Failed to read audit log for baseline", exc_info=True)
            return

        self._baseline.known_tools = known_tools
        self._baseline.output_lengths = deque(
            output_lengths[-_MAX_OUTPUT_SAMPLES:], maxlen=_MAX_OUTPUT_SAMPLES
        )
        self._baseline.recent_results = recent_results[-self.error_window_size :]
        self._tool_call_counts = tool_counts

        # Compute output length statistics (sample variance)
        if len(output_lengths) >= 2:
            mean = sum(output_lengths) / len(output_lengths)
            variance = sum((x - mean) ** 2 for x in output_lengths) / (
                len(output_lengths) - 1
            )
            self._baseline.output_length_mean = mean
            self._baseline.output_length_std = math.sqrt(variance)
        elif output_lengths:
            self._baseline.output_length_mean = float(output_lengths[0])
            self._baseline.output_length_std = 0.0

        logger.info(
            "Drift baseline built: %d known tools, %d output samples, mean_length=%.0f, std=%.0f",
            len(known_tools),
            len(output_lengths),
            self._baseline.output_length_mean,
            self._baseline.output_length_std,
        )

    def check_tool_call(self, tool_name: str) -> DriftAlert | None:
        """Check if a tool call is anomalous (never seen before).

        Returns a DriftAlert if the tool is new, None otherwise.
        Also updates internal tracking for future checks.
        """
        now = datetime.now(UTC).isoformat()

        count = self._tool_call_counts.get(tool_name, 0)
        self._tool_call_counts[tool_name] = count + 1

        if tool_name not in self._baseline.known_tools:
            if count <= self.new_tool_grace_count:
                alert = DriftAlert(
                    alert_type=DriftAlertType.NEW_TOOL,
                    tool_name=tool_name,
                    detail=f"Tool '{tool_name}' has never been used before "
                    f"(call #{count + 1}, grace={self.new_tool_grace_count})",
                    severity="critical",
                    timestamp=now,
                )
                logger.warning("DRIFT: %s", alert.detail)
                # Add to known tools after alerting so repeated calls
                # only alert up to grace_count
                if count >= self.new_tool_grace_count:
                    self._baseline.known_tools.add(tool_name)
                return alert

        return None

    def check_output_length(
        self, tool_name: str, output_length: int
    ) -> DriftAlert | None:
        """Check if a tool output length is anomalous.

        Returns a DriftAlert if the length deviates by more than
        z_threshold standard deviations from the baseline mean.
        """
        now = datetime.now(UTC).isoformat()

        # Need enough data to compute meaningful statistics
        if len(self._baseline.output_lengths) < 10:
            self._baseline.output_lengths.append(output_length)
            self._recompute_stats()
            return None

        mean = self._baseline.output_length_mean
        std = self._baseline.output_length_std

        # If std is 0 (all same length), any difference is anomalous
        if std == 0:
            self._baseline.output_lengths.append(output_length)
            self._recompute_stats()
            return None

        z_score = abs(output_length - mean) / std

        # Update baseline with new data point
        self._baseline.output_lengths.append(output_length)
        self._recompute_stats()

        if z_score > self.z_threshold:
            alert = DriftAlert(
                alert_type=DriftAlertType.RESPONSE_LENGTH_ANOMALY,
                tool_name=tool_name,
                detail=f"Output length {output_length} for '{tool_name}' has "
                f"z-score {z_score:.2f} (threshold={self.z_threshold}, "
                f"mean={mean:.0f}, std={std:.0f})",
                severity="warning",
                timestamp=now,
            )
            logger.warning("DRIFT: %s", alert.detail)
            return alert

        return None

    def check_error_rate(self, success: bool) -> DriftAlert | None:
        """Check if the error rate in the sliding window exceeds the threshold.

        Returns a DriftAlert if the error rate exceeds error_threshold.
        """
        now = datetime.now(UTC).isoformat()

        self._baseline.recent_results.append(success)

        # Keep only the window
        if len(self._baseline.recent_results) > self.error_window_size:
            self._baseline.recent_results = self._baseline.recent_results[
                -self.error_window_size :
            ]

        # Need minimum sample to avoid false positives
        if len(self._baseline.recent_results) < 10:
            return None

        error_count = sum(1 for r in self._baseline.recent_results if not r)
        error_rate = error_count / len(self._baseline.recent_results)

        if error_rate > self.error_threshold:
            alert = DriftAlert(
                alert_type=DriftAlertType.ERROR_RATE_SPIKE,
                detail=f"Error rate {error_rate:.1%} exceeds threshold "
                f"{self.error_threshold:.1%} "
                f"({error_count}/{len(self._baseline.recent_results)} "
                f"in sliding window)",
                severity="warning",
                timestamp=now,
            )
            logger.warning("DRIFT: %s", alert.detail)
            return alert

        return None

    def check_all(
        self,
        tool_name: str,
        output_length: int,
        success: bool,
    ) -> list[DriftAlert]:
        """Run all drift checks and return any alerts.

        Convenience method that runs all three checks and returns
        a list of all triggered alerts.
        """
        alerts: list[DriftAlert] = []

        alert = self.check_tool_call(tool_name)
        if alert:
            alerts.append(alert)

        alert = self.check_output_length(tool_name, output_length)
        if alert:
            alerts.append(alert)

        alert = self.check_error_rate(success)
        if alert:
            alerts.append(alert)

        return alerts

    def _recompute_stats(self) -> None:
        """Recompute output length statistics from current data (sample variance)."""
        lengths = self._baseline.output_lengths
        if len(lengths) < 2:
            if lengths:
                self._baseline.output_length_mean = float(lengths[0])
            self._baseline.output_length_std = 0.0
            return

        mean = sum(lengths) / len(lengths)
        variance = sum((x - mean) ** 2 for x in lengths) / (len(lengths) - 1)
        self._baseline.output_length_mean = mean
        self._baseline.output_length_std = math.sqrt(variance)
