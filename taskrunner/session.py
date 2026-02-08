"""Session manager - persistent conversation sessions backed by JSON files."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """A conversation session with message history."""

    sender_id: str
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


class SessionManager:
    """Manages conversation sessions persisted as JSON files."""

    def __init__(self, sessions_dir: str = "sessions", max_history: int = 50):
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_history = max_history

    def get_or_create(self, sender_id: str) -> Session:
        """Load session from disk or create a new one."""
        session = self._load(sender_id)
        if session is None:
            session = Session(sender_id=sender_id)
            logger.info("Created new session for %s", sender_id)
        return session

    def add_user_message(self, sender_id: str, text: str) -> Session:
        """Add a user message, save to disk, and return the updated session."""
        session = self.get_or_create(sender_id)
        session.messages.append({"role": "user", "content": text})
        session.last_active = time.time()
        self._save(session)
        return session

    def add_assistant_response(self, sender_id: str, content: list) -> None:
        """Add an assistant response (may include tool_use blocks), save."""
        session = self.get_or_create(sender_id)
        session.messages.append({"role": "assistant", "content": content})
        session.last_active = time.time()
        self._save(session)

    def add_tool_results(self, sender_id: str, results: list[dict]) -> None:
        """Add tool results as a user message, save."""
        session = self.get_or_create(sender_id)
        session.messages.append({"role": "user", "content": results})
        session.last_active = time.time()
        self._save(session)

    def clear(self, sender_id: str) -> None:
        """Clear session history (delete JSON file)."""
        path = self._session_path(sender_id)
        if path.exists():
            path.unlink()
            logger.info("Cleared session for %s", sender_id)

    def _save(self, session: Session) -> None:
        """Write session to disk, trimming to max_history."""
        # Trim old messages (keep most recent)
        if len(session.messages) > self._max_history:
            session.messages = session.messages[-self._max_history:]

        data = {
            "sender_id": session.sender_id,
            "created_at": session.created_at,
            "last_active": session.last_active,
            "messages": session.messages,
        }

        path = self._session_path(session.sender_id)
        path.write_text(json.dumps(data, indent=2))

    def _load(self, sender_id: str) -> Session | None:
        """Load session from disk if it exists."""
        path = self._session_path(sender_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
            session = Session(
                sender_id=data["sender_id"],
                messages=data.get("messages", []),
                created_at=data.get("created_at", time.time()),
                last_active=data.get("last_active", time.time()),
            )
            # Trim on load
            if len(session.messages) > self._max_history:
                session.messages = session.messages[-self._max_history:]
            return session
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Corrupt session file for %s, resetting: %s", sender_id, e)
            return None

    def _session_path(self, sender_id: str) -> Path:
        """Get the filesystem path for a sender's session file."""
        safe_name = _sanitize_sender_id(sender_id)
        return self._dir / f"{safe_name}.json"


def _sanitize_sender_id(sender_id: str) -> str:
    """Sanitize a sender ID for use as a filename.

    Phone numbers -> digits only. Other IDs -> alphanumeric + underscore.
    """
    # Strip everything except alphanumeric and underscore
    sanitized = re.sub(r"[^\w]", "", sender_id)
    return sanitized or "unknown"
