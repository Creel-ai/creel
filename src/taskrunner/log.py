"""Structured logging with optional JSON output and request ID correlation."""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from datetime import UTC, datetime

# Context variable for threading request IDs through async call chains
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def generate_request_id() -> str:
    """Generate a short request ID (first 8 chars of a UUID4)."""
    return uuid.uuid4().hex[:8]


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Include request_id from context var if set
        rid = request_id_var.get(None)
        if rid is not None:
            entry["request_id"] = rid

        # Include any extra fields passed via `extra`
        for key in record.__dict__:
            if key not in logging.LogRecord(
                "", 0, "", 0, "", (), None
            ).__dict__ and key not in (
                "message",
                "args",
            ):
                entry[key] = record.__dict__[key]

        return json.dumps(entry, default=str)


# Standard keys on a LogRecord — precomputed for performance
_STANDARD_KEYS: set[str] | None = None


def _standard_keys() -> set[str]:
    global _STANDARD_KEYS
    if _STANDARD_KEYS is None:
        _STANDARD_KEYS = set(
            logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
        ) | {
            "message",
            "args",
        }
    return _STANDARD_KEYS


class _JSONFormatterOpt(logging.Formatter):
    """Optimised JSON formatter (avoids creating a LogRecord each call)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        rid = request_id_var.get(None)
        if rid is not None:
            entry["request_id"] = rid

        std = _standard_keys()
        for key, value in record.__dict__.items():
            if key not in std:
                entry[key] = value

        return json.dumps(entry, default=str)


def setup_logging(json_mode: bool = False, level: str = "INFO") -> None:
    """Configure the root logger.

    Args:
        json_mode: If True, output structured JSON lines.
        level: Log level name (e.g. "INFO", "DEBUG").
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates
    root.handlers.clear()

    handler = logging.StreamHandler()

    if json_mode:
        handler.setFormatter(_JSONFormatterOpt())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root.addHandler(handler)
