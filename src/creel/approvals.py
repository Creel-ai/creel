"""Async approval queue for REVIEW verdict actions."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PendingAction:
    """A tool call awaiting user approval."""

    id: str
    tool_name: str
    tool_input: dict
    sender_id: str
    policy_reason: str
    created_at: str  # ISO format
    status: str = "pending"  # pending | approved | denied | expired
    tool_use_id: str = ""

    @staticmethod
    def create(
        sender_id: str,
        tool_name: str,
        tool_input: dict,
        reason: str,
        tool_use_id: str = "",
    ) -> PendingAction:
        return PendingAction(
            id=uuid.uuid4().hex[:8],
            tool_name=tool_name,
            tool_input=tool_input,
            sender_id=sender_id,
            policy_reason=reason,
            created_at=datetime.now(UTC).isoformat(),
            tool_use_id=tool_use_id,
        )


class ApprovalQueue:
    """Persistent queue of pending approval actions backed by a JSON file."""

    def __init__(self, approvals_dir: str = "approvals"):
        self._dir = Path(approvals_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "pending.json"
        self._actions: dict[str, PendingAction] = {}
        self._load()

    # -- public API --

    def add(
        self,
        sender_id: str,
        tool_name: str,
        tool_input: dict,
        reason: str,
        tool_use_id: str = "",
    ) -> PendingAction:
        action = PendingAction.create(sender_id, tool_name, tool_input, reason, tool_use_id=tool_use_id)
        self._actions[action.id] = action
        self._save()
        logger.info("Queued pending action %s: %s for %s", action.id, tool_name, sender_id)
        return action

    def get_pending(self, sender_id: str) -> PendingAction | None:
        """Get the most recent pending action for a sender."""
        pending = [
            a for a in self._actions.values() if a.sender_id == sender_id and a.status == "pending"
        ]
        if not pending:
            return None
        # Most recent first
        pending.sort(key=lambda a: a.created_at, reverse=True)
        return pending[0]

    def resolve(self, action_id: str, approved: bool) -> None:
        action = self._actions.get(action_id)
        if action is None:
            raise ValueError(f"No action with id {action_id}")
        action.status = "approved" if approved else "denied"
        self._save()
        logger.info("Resolved action %s: %s", action_id, action.status)

    def get_resolved(self, action_id: str) -> PendingAction | None:
        action = self._actions.get(action_id)
        if action and action.status in ("approved", "denied"):
            return action
        return None

    def cleanup(self, max_age_hours: int = 24) -> int:
        """Remove old resolved/expired actions. Returns count removed."""
        now = datetime.now(UTC)
        to_remove = []
        for aid, action in self._actions.items():
            created = datetime.fromisoformat(action.created_at)
            age_hours = (now - created).total_seconds() / 3600
            if age_hours > max_age_hours:
                if action.status == "pending":
                    action.status = "expired"
                to_remove.append(aid)
        for aid in to_remove:
            del self._actions[aid]
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
                self._actions[item["id"]] = PendingAction(**item)
        except Exception:
            logger.exception("Failed to load approvals from %s", self._path)

    def _save(self) -> None:
        data = [asdict(a) for a in self._actions.values()]
        self._path.write_text(json.dumps(data, indent=2) + "\n")
