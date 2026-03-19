"""Three-mode sender policy engine for channel integrations."""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from creel.channels.sender_store import SenderRecord, SenderStore

logger = logging.getLogger(__name__)


class SenderPolicy(enum.Enum):
    CLOSED = "closed"
    ALLOWLIST = "allowlist"
    OPEN = "open"


@dataclass
class GateResult:
    allowed: bool
    pending: bool = False
    sender_record: SenderRecord | None = None


class SenderGate:
    """Policy engine that decides whether a sender may interact with the agent.

    Three modes:
    - **CLOSED**: only static senders pass (current default behaviour).
    - **OPEN**: anyone may talk (dev/testing).
    - **ALLOWLIST**: static senders pass; unknown senders are queued for owner
      approval; the owner is notified via ``notify_fn``.
    """

    def __init__(
        self,
        *,
        policy: SenderPolicy,
        static_senders: set[str],
        store: SenderStore,
        owner_sender_ids: set[str],
        notify_fn: Callable[[str, str], None],
        auto_approve: bool = False,
    ) -> None:
        self._policy = policy
        self._static_senders = static_senders
        self._store = store
        self._owner_ids = owner_sender_ids
        self._notify = notify_fn
        self._auto_approve = auto_approve

    @property
    def policy(self) -> SenderPolicy:
        return self._policy

    # --- main decision point ---

    def check(
        self,
        sender_id: str,
        display_name: str = "",
        text: str = "",
    ) -> GateResult:
        """Return whether *sender_id* is allowed to send messages."""
        if self._policy == SenderPolicy.OPEN:
            return GateResult(allowed=True)

        if self._policy == SenderPolicy.CLOSED:
            return GateResult(allowed=sender_id in self._static_senders)

        # ALLOWLIST mode
        if sender_id in self._static_senders:
            return GateResult(allowed=True)

        # Check store
        record = self._store.get(sender_id)

        if record is not None:
            if record.status == "approved":
                return GateResult(allowed=True, sender_record=record)
            if record.status == "denied":
                return GateResult(allowed=False, sender_record=record)
            # pending — hold the message
            self._store.hold_message(sender_id, {"sender_id": sender_id, "text": text})
            return GateResult(allowed=False, pending=True, sender_record=record)

        # Unknown sender — auto-approve or queue
        if self._auto_approve:
            self._store.add_pending(sender_id, display_name)
            approved_record = self._store.approve(sender_id, resolved_by="auto")
            return GateResult(allowed=True, sender_record=approved_record)

        # Queue as pending, notify owners
        new_record = self._store.add_pending(sender_id, display_name)
        self._store.hold_message(sender_id, {"sender_id": sender_id, "text": text})
        self._notify_owners(sender_id, display_name)
        return GateResult(allowed=False, pending=True, sender_record=new_record)

    # --- owner command handling ---

    _CMD_RE = re.compile(r"^/(approve|deny|pending)\s*(\S+)?", re.IGNORECASE)

    def handle_owner_response(self, text: str, sender_id: str) -> str | None:
        """Parse ``/approve``, ``/deny``, ``/pending`` commands from the owner.

        Returns a status message string if the text was a gate command,
        or ``None`` if it wasn't (so normal dispatch can proceed).
        """
        if sender_id not in self._owner_ids:
            return None

        m = self._CMD_RE.match(text.strip())
        if not m:
            return None

        cmd = m.group(1).lower()
        target = m.group(2)

        if cmd == "pending":
            pending = self._store.get_pending()
            if not pending:
                return "No pending senders."
            lines = [f"- {r.sender_id} ({r.display_name or 'unknown'})" for r in pending]
            return "Pending senders:\n" + "\n".join(lines)

        if not target:
            return f"Usage: /{cmd} <sender_id>"

        if cmd == "approve":
            record = self._store.approve(target, resolved_by=sender_id)
            if record is None:
                return f"No pending sender with id {target}."
            return f"Approved sender {target}."

        if cmd == "deny":
            record = self._store.deny(target, resolved_by=sender_id)
            if record is None:
                return f"No pending sender with id {target}."
            return f"Denied sender {target}."

        return None  # pragma: no cover

    def release_held_messages(self, sender_id: str) -> list[dict]:
        """Return and clear held messages for *sender_id*."""
        return self._store.release_held_messages(sender_id)

    # --- internals ---

    def _notify_owners(self, sender_id: str, display_name: str) -> None:
        label = f"{display_name} " if display_name else ""
        msg = (
            f"New sender {label}(id: {sender_id}) wants to chat. "
            f"/approve {sender_id} or /deny {sender_id}"
        )
        for owner_id in self._owner_ids:
            try:
                self._notify(owner_id, msg)
            except Exception:
                logger.exception("Failed to notify owner %s", owner_id)
