"""Persistent sender state per channel — JSON-file-backed storage."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SenderRecord:
    """A sender's approval state."""

    sender_id: str
    status: str = "pending"  # pending | approved | denied
    display_name: str = ""
    created_at: str = ""  # ISO format
    resolved_at: str = ""
    resolved_by: str = ""
    held_messages: list[dict] = field(default_factory=list)


class SenderStore:
    """JSON-file-backed persistence for sender approval records.

    Follows the same pattern as ``ApprovalQueue`` in ``creel/approvals.py``.
    Thread-safe via ``threading.Lock`` for webhook safety.
    """

    def __init__(self, store_dir: str | Path, channel_id: str) -> None:
        self._dir = Path(store_dir) / channel_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "senders.json"
        self._lock = threading.Lock()
        self._records: dict[str, SenderRecord] = {}
        self._load()

    # -- public API --

    def get(self, sender_id: str) -> SenderRecord | None:
        with self._lock:
            return self._records.get(sender_id)

    def add_pending(self, sender_id: str, display_name: str = "") -> SenderRecord:
        with self._lock:
            existing = self._records.get(sender_id)
            if existing:
                return existing
            record = SenderRecord(
                sender_id=sender_id,
                status="pending",
                display_name=display_name,
                created_at=datetime.now(UTC).isoformat(),
            )
            self._records[sender_id] = record
            self._save()
            logger.info("Added pending sender %s (%s)", sender_id, display_name)
            return record

    def hold_message(self, sender_id: str, message: dict) -> None:
        with self._lock:
            record = self._records.get(sender_id)
            if record is None:
                return
            record.held_messages.append(message)
            self._save()

    def approve(self, sender_id: str, resolved_by: str = "") -> SenderRecord | None:
        with self._lock:
            record = self._records.get(sender_id)
            if record is None:
                return None
            record.status = "approved"
            record.resolved_at = datetime.now(UTC).isoformat()
            record.resolved_by = resolved_by
            self._save()
            logger.info("Approved sender %s (by %s)", sender_id, resolved_by)
            return record

    def deny(self, sender_id: str, resolved_by: str = "") -> SenderRecord | None:
        with self._lock:
            record = self._records.get(sender_id)
            if record is None:
                return None
            record.status = "denied"
            record.resolved_at = datetime.now(UTC).isoformat()
            record.resolved_by = resolved_by
            self._save()
            logger.info("Denied sender %s (by %s)", sender_id, resolved_by)
            return record

    def is_approved(self, sender_id: str) -> bool:
        with self._lock:
            record = self._records.get(sender_id)
            return record is not None and record.status == "approved"

    def is_denied(self, sender_id: str) -> bool:
        with self._lock:
            record = self._records.get(sender_id)
            return record is not None and record.status == "denied"

    def get_pending(self) -> list[SenderRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.status == "pending"]

    def release_held_messages(self, sender_id: str) -> list[dict]:
        with self._lock:
            record = self._records.get(sender_id)
            if record is None:
                return []
            msgs = list(record.held_messages)
            record.held_messages = []
            self._save()
            return msgs

    def cleanup(self, max_age_hours: int = 24) -> int:
        """Remove old denied/pending records. Returns count removed."""
        now = datetime.now(UTC)
        to_remove: list[str] = []
        with self._lock:
            for sid, record in self._records.items():
                if not record.created_at:
                    continue
                created = datetime.fromisoformat(record.created_at)
                age_hours = (now - created).total_seconds() / 3600
                if age_hours > max_age_hours and record.status in ("pending", "denied"):
                    to_remove.append(sid)
            for sid in to_remove:
                del self._records[sid]
            if to_remove:
                self._save()
        return len(to_remove)

    # -- persistence --

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for item in data:
                self._records[item["sender_id"]] = SenderRecord(**item)
        except Exception:
            logger.exception("Failed to load sender records from %s", self._path)

    def _save(self) -> None:
        data = [asdict(r) for r in self._records.values()]
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self._path)
