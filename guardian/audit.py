"""Audit logger — append-only JSONL log with privacy-preserving hashes."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from taskrunner.log import request_id_var

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
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        self._write({
            "event": "screen_input",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input_hash": input_hash,
            "input_length": input_length,
            "blocked": blocked,
            "source": source,
            "confidence": confidence,
        })

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
        self._write({
            "event": "screen_tool_result",
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "text": text,
            "blocked": blocked,
            "source": source,
            "confidence": confidence,
        })

    def log_screen_debug(
        self,
        *,
        text: str,
        chunks: list[dict],
        blocked: bool,
        source: str,
    ) -> None:
        """Log a debug screening event with raw text and per-chunk breakdown."""
        self._write({
            "event": "screen_input_debug",
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "chunks": chunks,
            "blocked": blocked,
            "source": source,
        })

    def log_action(
        self,
        *,
        tool_name: str,
        arg_keys: list[str],
        verdict: str,
        matched_rule: str,
    ) -> None:
        """Log an action validation event."""
        self._write({
            "event": "validate_action",
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "arg_keys": arg_keys,
            "verdict": verdict,
            "matched_rule": matched_rule,
        })

    def log_action_outcome(
        self,
        *,
        tool_name: str,
        verdict: str,
        outcome: str,
    ) -> None:
        """Log the outcome of a review/deny action."""
        self._write({
            "event": "action_outcome",
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "verdict": verdict,
            "outcome": outcome,
        })

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
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "success": success,
            "duration_ms": round(duration_ms, 1),
            "output_length": output_length,
        }
        if error:
            record["error"] = error[:200]
        self._write(record)


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
