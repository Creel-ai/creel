"""Audit logger — append-only JSONL log with privacy-preserving hashes."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

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
    """

    def __init__(self, log_file: str | Path) -> None:
        self._path = Path(log_file)

    def _write(self, record: dict) -> None:
        """Append a JSON record to the log file."""
        try:
            with open(self._path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            logger.warning("Failed to write audit log to %s", self._path, exc_info=True)

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
