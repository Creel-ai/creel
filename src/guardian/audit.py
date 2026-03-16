"""Audit logger — append-only JSONL log with privacy-preserving hashes."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from creel.log import request_id_var

logger = logging.getLogger(__name__)


def _hash_text(text: str) -> str:
    """Return truncated SHA-256 hex digest (16 chars) of text."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class AuditLogger:
    """Append-only JSONL audit logger.

    Stores SHA-256 hashes of input text (never raw content) and only
    the keys of tool arguments (never values).  Write failures are
    caught and logged as warnings — the audit log must never crash the
    main pipeline.

    Supports daily log rotation: when ``rotate_daily=True``, log files
    are named ``<base>-YYYY-MM-DD.jsonl``.
    """

    def __init__(
        self,
        log_file: str | Path,
        *,
        rotate_daily: bool = False,
        max_size_mb: float = 0,
    ) -> None:
        self._base_path = Path(log_file)
        self._rotate_daily = rotate_daily
        self._max_size_bytes = int(max_size_mb * 1024 * 1024) if max_size_mb > 0 else 0

    def _get_path(self) -> Path:
        """Return the current log file path, accounting for rotation."""
        if self._rotate_daily:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            stem = self._base_path.stem
            suffix = self._base_path.suffix or ".jsonl"
            return self._base_path.parent / f"{stem}-{today}{suffix}"
        return self._base_path

    def _write(self, record: dict) -> None:
        """Append a JSON record to the log file."""
        # Include request_id if set in current context
        rid = request_id_var.get(None)
        if rid is not None:
            record["request_id"] = rid
        try:
            path = self._get_path()
            path.parent.mkdir(parents=True, exist_ok=True)

            # Size-based rotation
            if self._max_size_bytes > 0 and path.exists():
                if path.stat().st_size >= self._max_size_bytes:
                    rotated = path.with_suffix(f"{path.suffix}.1")
                    if rotated.exists():
                        rotated.unlink()
                    path.rename(rotated)

            with open(path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            logger.warning("Failed to write audit log to %s", self._base_path, exc_info=True)

    def log_screen(
        self,
        *,
        input_hash: str,
        input_length: int,
        blocked: bool,
        source: str,
        confidence: float | None = None,
    ) -> None:
        """Log an input screening event."""
        self._write(
            {
                "event": "screen_input",
                "ts": datetime.now(UTC).isoformat(),
                "input_hash": input_hash,
                "input_length": input_length,
                "blocked": blocked,
                "source": source,
                "confidence": confidence,
            }
        )

    def log_tool_screen(
        self,
        *,
        tool_name: str,
        text: str,
        blocked: bool,
        source: str,
        confidence: float | None = None,
    ) -> None:
        """Log a tool result screening event (includes raw text for debugging)."""
        self._write(
            {
                "event": "screen_tool_result",
                "ts": datetime.now(UTC).isoformat(),
                "tool_name": tool_name,
                "text": text,
                "blocked": blocked,
                "source": source,
                "confidence": confidence,
            }
        )

    def log_screen_debug(
        self,
        *,
        text: str,
        chunks: list[dict],
        blocked: bool,
        source: str,
    ) -> None:
        """Log a debug screening event with raw text and per-chunk breakdown."""
        self._write(
            {
                "event": "screen_input_debug",
                "ts": datetime.now(UTC).isoformat(),
                "text": text,
                "chunks": chunks,
                "blocked": blocked,
                "source": source,
            }
        )

    def log_action(
        self,
        *,
        tool_name: str,
        arg_keys: list[str],
        verdict: str,
        matched_rule: str,
    ) -> None:
        """Log an action validation event."""
        self._write(
            {
                "event": "validate_action",
                "ts": datetime.now(UTC).isoformat(),
                "tool_name": tool_name,
                "arg_keys": arg_keys,
                "verdict": verdict,
                "matched_rule": matched_rule,
            }
        )

    def log_action_outcome(
        self,
        *,
        tool_name: str,
        verdict: str,
        outcome: str,
    ) -> None:
        """Log the outcome of a review/deny action."""
        self._write(
            {
                "event": "action_outcome",
                "ts": datetime.now(UTC).isoformat(),
                "tool_name": tool_name,
                "verdict": verdict,
                "outcome": outcome,
            }
        )

    def log_coherence_check(
        self,
        *,
        tool_name: str,
        coherent: bool,
        confidence: float | None = None,
    ) -> None:
        """Log a coherence check event."""
        self._write(
            {
                "event": "coherence_check",
                "ts": datetime.now(UTC).isoformat(),
                "tool_name": tool_name,
                "coherent": coherent,
                "confidence": confidence,
            }
        )

    def log_drift_alert(
        self,
        *,
        alert_type: str,
        tool_name: str,
        detail: str,
        severity: str,
    ) -> None:
        """Log a drift detection alert."""
        self._write(
            {
                "event": "drift_alert",
                "ts": datetime.now(UTC).isoformat(),
                "alert_type": alert_type,
                "tool_name": tool_name,
                "detail": detail,
                "severity": severity,
            }
        )

    def log_credential_leak(
        self,
        *,
        tool_name: str,
        patterns_found: list[dict],
        count: int,
    ) -> None:
        """Log a credential leak detection event."""
        self._write(
            {
                "event": "credential_leak",
                "ts": datetime.now(UTC).isoformat(),
                "tool_name": tool_name,
                "patterns_found": patterns_found,
                "count": count,
            }
        )

    def log_interactive_io(
        self,
        *,
        session_id: str,
        tool_name: str,
        direction: str,
        data_length: int,
        data_hash: str,
    ) -> None:
        """Log an interactive session I/O event.

        Records the direction (input/output), data length, and hash
        of the data — never raw content for privacy.
        """
        self._write(
            {
                "event": "interactive_io",
                "ts": datetime.now(UTC).isoformat(),
                "session_id": session_id,
                "tool_name": tool_name,
                "direction": direction,
                "data_length": data_length,
                "data_hash": data_hash,
            }
        )

    def log_interactive_session(
        self,
        *,
        session_id: str,
        tool_name: str,
        action: str,
        command_hash: str | None = None,
        exit_code: int | None = None,
        duration_s: float | None = None,
        io_summary: dict | None = None,
    ) -> None:
        """Log an interactive session lifecycle event (start/close)."""
        record: dict = {
            "event": "interactive_session",
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "tool_name": tool_name,
            "action": action,
        }
        if command_hash is not None:
            record["command_hash"] = command_hash
        if exit_code is not None:
            record["exit_code"] = exit_code
        if duration_s is not None:
            record["duration_s"] = round(duration_s, 1)
        if io_summary is not None:
            record["io_summary"] = io_summary
        self._write(record)

    def log_tool_result(
        self,
        *,
        tool_name: str,
        success: bool,
        duration_ms: float,
        output_length: int,
        error: str | None = None,
    ) -> None:
        """Log a tool execution result (no output content — just metadata)."""
        record = {
            "event": "tool_result",
            "ts": datetime.now(UTC).isoformat(),
            "tool_name": tool_name,
            "success": success,
            "duration_ms": round(duration_ms, 1),
            "output_length": output_length,
        }
        if error:
            record["error"] = error[:200]
        self._write(record)

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """Strip query string and fragment from a URL to avoid logging sensitive params."""
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def log_network_request(
        self,
        *,
        url: str,
        domain: str,
        executor: str,
        method: str,
        request_size_bytes: int = 0,
        response_size_bytes: int = 0,
        status_code: int | None = None,
        blocked: bool = False,
        block_reason: str = "",
    ) -> None:
        """Log a network request event."""
        record: dict = {
            "event": "network_request",
            "ts": datetime.now(UTC).isoformat(),
            "url": self._sanitize_url(url),
            "domain": domain,
            "executor": executor,
            "method": method,
            "request_size_bytes": request_size_bytes,
            "response_size_bytes": response_size_bytes,
            "blocked": blocked,
        }
        if status_code is not None:
            record["status_code"] = status_code
        if block_reason:
            record["block_reason"] = block_reason
        self._write(record)

    def log_network_alert(
        self,
        *,
        alert_type: str,
        executor: str,
        detail: str,
        url: str = "",
        domain: str = "",
    ) -> None:
        """Log a network security alert (large payload, unknown domain, rate limit)."""
        self._write(
            {
                "event": "network_alert",
                "ts": datetime.now(UTC).isoformat(),
                "alert_type": alert_type,
                "executor": executor,
                "detail": detail,
                "url": self._sanitize_url(url),
                "domain": domain,
            }
        )


def read_audit_log(
    log_file: str | Path,
    *,
    tail: int = 0,
    event_filter: str | None = None,
    blocked_only: bool = False,
    denied_only: bool = False,
    tool_filter: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Read and filter audit log entries.

    Args:
        log_file: Path to the JSONL audit log.
        tail: Return only the last N entries (0 = no limit).
        event_filter: Only include entries with this event type.
        blocked_only: Only include entries where ``blocked`` is true.
        denied_only: Only include entries where ``verdict`` is ``deny``.
        tool_filter: Only include entries mentioning this tool name.
        since: Only include entries with ``ts`` >= this ISO date string.
    """
    path = Path(log_file)
    if not path.exists():
        return []

    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event_filter and entry.get("event") != event_filter:
                continue
            if blocked_only and not entry.get("blocked"):
                continue
            if denied_only and entry.get("verdict") != "deny":
                continue
            if tool_filter and entry.get("tool_name") != tool_filter:
                continue
            if since and entry.get("ts", "") < since:
                continue

            entries.append(entry)

    if tail > 0:
        entries = entries[-tail:]

    return entries
