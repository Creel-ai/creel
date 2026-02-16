"""Session manager - persistent conversation sessions backed by JSON files."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_ACTIVE_INDEX_FILE = "_active.json"


@dataclass
class Session:
    """A conversation session with message history."""

    sender_id: str
    session_id: str = field(default_factory=lambda: secrets.token_hex(4))
    title: str = ""
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    summary: str = ""
    token_count: int = 0


class SessionManager:
    """Manages conversation sessions persisted as JSON files."""

    def __init__(
        self,
        sessions_dir: str = "sessions",
        max_history: int = 50,
        ttl_hours: float = 0,
        summarize_on_trim: bool = False,
        summarize_fn: Callable[[list[dict]], str] | None = None,
        max_context_tokens: int = 180_000,
    ):
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_history = max_history
        self._ttl_seconds = ttl_hours * 3600 if ttl_hours > 0 else 0
        self._summarize_on_trim = summarize_on_trim
        self._summarize_fn = summarize_fn
        self._max_context_tokens = max_context_tokens

    # -- public API --

    def get_or_create(self, sender_id: str) -> Session:
        """Load the active session from the index, or create a new one.

        If TTL is configured and the active session has expired, a new session
        is created automatically.
        """
        active_id = self._get_active_session_id(sender_id)
        if active_id:
            session = self._load(active_id)
            if session is not None:
                if self._is_expired(session):
                    logger.info(
                        "Session %s expired (last_active=%.0f), starting new session",
                        session.session_id, session.last_active,
                    )
                else:
                    return session

        # No active session (or file missing or expired) — create fresh
        session = Session(sender_id=sender_id)
        self._set_active_session_id(sender_id, session.session_id)
        logger.info("Created new session %s for %s", session.session_id, sender_id)
        return session

    def _is_expired(self, session: Session) -> bool:
        """Check if a session has exceeded the TTL."""
        if not self._ttl_seconds:
            return False
        return (time.time() - session.last_active) > self._ttl_seconds

    def cleanup_expired(self, sender_id: str) -> int:
        """Remove expired sessions for a sender. Returns count of removed sessions."""
        if not self._ttl_seconds:
            return 0
        removed = 0
        for path in self._dir.glob("*.json"):
            if path.name == _ACTIVE_INDEX_FILE:
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("sender_id") != sender_id:
                continue
            last_active = data.get("last_active", 0)
            if (time.time() - last_active) > self._ttl_seconds:
                path.unlink()
                removed += 1
                logger.info("Removed expired session %s", path.stem)
        return removed

    def new_session(self, sender_id: str) -> Session:
        """Save the current session and start a fresh one."""
        # Save any existing active session first
        active_id = self._get_active_session_id(sender_id)
        if active_id:
            existing = self._load(active_id)
            if existing is not None:
                self._save(existing)

        session = Session(sender_id=sender_id)
        self._save(session)
        self._set_active_session_id(sender_id, session.session_id)
        logger.info("Started new session %s for %s", session.session_id, sender_id)
        return session

    def list_sessions(self, sender_id: str) -> list[dict]:
        """Return metadata for all sessions belonging to a sender, sorted by last_active desc."""
        results = []
        for path in self._dir.glob("*.json"):
            if path.name == _ACTIVE_INDEX_FILE:
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("sender_id") != sender_id:
                continue
            results.append({
                "session_id": data.get("session_id", path.stem),
                "title": data.get("title", ""),
                "created_at": data.get("created_at", 0),
                "last_active": data.get("last_active", 0),
                "message_count": len(data.get("messages", [])),
            })
        results.sort(key=lambda r: r["last_active"], reverse=True)
        return results

    def resume_session(self, sender_id: str, session_id: str) -> Session:
        """Switch the active session to an existing one.

        Raises ValueError if the session doesn't exist or belongs to another sender.
        """
        session = self._load(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        if session.sender_id != sender_id:
            raise ValueError(f"Session {session_id} not found")
        self._set_active_session_id(sender_id, session_id)
        logger.info("Resumed session %s for %s", session_id, sender_id)
        return session

    def add_user_message(self, sender_id: str, text: str) -> Session:
        """Add a user message, save to disk, and return the updated session."""
        session = self.get_or_create(sender_id)
        session.messages.append({"role": "user", "content": text})
        session.last_active = time.time()
        # Set title from the first user message
        if not session.title:
            session.title = text[:60].strip()
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

    def save_session(self, session: Session) -> None:
        """Save a session to disk (public API).

        Trims messages to max_history and persists to the session file.
        """
        self._save(session)

    def clear(self, sender_id: str) -> None:
        """Clear the active session's messages (keeps file, resets messages)."""
        active_id = self._get_active_session_id(sender_id)
        if active_id:
            session = self._load(active_id)
            if session is not None:
                session.messages = []
                session.title = ""
                self._save(session)
                logger.info("Cleared session %s for %s", active_id, sender_id)
                return
        logger.info("No active session to clear for %s", sender_id)

    # -- compaction --

    def update_token_count(self, sender_id: str, input_tokens: int) -> None:
        """Update the session's token count and trigger compaction if needed.

        Called after each LLM response with the input_tokens from usage data.
        """
        session = self.get_or_create(sender_id)
        session.token_count = input_tokens

        if (
            self._summarize_on_trim
            and session.token_count >= self._max_context_tokens
            and len(session.messages) > 2
        ):
            self._compact_with_summary(session)
            self._save(session)

    def _compact_with_summary(self, session: Session) -> None:
        """Compact older messages into a summary, keeping recent messages."""
        if not self._summarize_fn:
            logger.warning("Compaction requested but no summarize_fn configured, falling back to trim")
            session.messages = self._trim_preserving_tool_pairs(
                session.messages, self._max_history,
            )
            return

        keep_count = len(session.messages) // 2
        split_idx = self._find_safe_split(session.messages, len(session.messages) - keep_count)
        older = session.messages[:split_idx]
        recent = session.messages[split_idx:]

        if not older:
            return

        try:
            summary_text = self._summarize_fn(older)
        except Exception:
            logger.warning("Summarization failed, falling back to trim", exc_info=True)
            session.messages = self._trim_preserving_tool_pairs(
                session.messages, self._max_history,
            )
            return

        summary_msg = {
            "role": "user",
            "content": (
                "[CONVERSATION SUMMARY]\n"
                f"<summary>\n{summary_text}\n</summary>"
            ),
        }

        session.messages = [summary_msg] + recent
        session.summary = summary_text
        session.token_count = 0
        logger.info(
            "Compacted session %s: %d messages -> 1 summary + %d recent",
            session.session_id, len(older) + len(recent), len(recent),
        )

    @staticmethod
    def _find_safe_split(messages: list[dict], target_idx: int) -> int:
        """Find a safe split point at or after target_idx (a user text message boundary).

        Avoids splitting in the middle of a tool-call pair.
        """
        for i in range(target_idx, len(messages)):
            msg = messages[i]
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return i
        # If no safe point found after target, search before
        for i in range(target_idx - 1, 0, -1):
            msg = messages[i]
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return i
        return target_idx

    # -- persistence --

    @staticmethod
    def _ensure_valid_start(messages: list[dict]) -> list[dict]:
        """Strip orphaned messages so the list starts with a user text message.

        After trimming by max_history, the list may begin mid-tool-call
        (e.g. with an assistant tool_use whose tool_result comes next, or
        a user tool_result whose matching tool_use was trimmed away).

        This method finds the first position where a complete,
        non-tool-call user text message begins and drops everything before it.
        This ensures we never start mid-tool-call.
        """
        # Find the first user text message that is NOT followed by being
        # part of an orphaned tool-call sequence.  The simplest safe rule:
        # walk forward until we find a user message with string content
        # that is NOT a tool_result.
        while messages:
            msg = messages[0]
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                break
            messages.pop(0)
        return messages

    @staticmethod
    def _trim_preserving_tool_pairs(messages: list[dict], max_history: int) -> list[dict]:
        """Trim messages to max_history while keeping complete tool-call pairs.

        A tool-call sequence is:
          1. assistant message with tool_use block(s)
          2. user message with tool_result block(s)

        We never split these apart. After naive trimming, we scan from the
        start to find the first safe boundary (a user text message not part
        of an incomplete tool-call pair).
        """
        if len(messages) <= max_history:
            return messages

        # Start with naive trim from the end
        trimmed = messages[-max_history:]

        # Now find first safe start point
        i = 0
        while i < len(trimmed):
            msg = trimmed[i]
            # A user message with string content (not tool_result) is safe
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                break
            # An assistant message with only text blocks (no tool_use) is safe
            # if it's followed by a user text message, but we prefer starting
            # with user messages for API compatibility
            i += 1

        return trimmed[i:]

    def _save(self, session: Session) -> None:
        """Write session to disk, trimming to max_history as safety net."""
        if len(session.messages) > self._max_history:
            session.messages = self._trim_preserving_tool_pairs(
                session.messages, self._max_history,
            )

        data = {
            "session_id": session.session_id,
            "sender_id": session.sender_id,
            "title": session.title,
            "created_at": session.created_at,
            "last_active": session.last_active,
            "messages": session.messages,
            "summary": session.summary,
            "token_count": session.token_count,
        }

        path = self._session_path(session.session_id)
        path.write_text(json.dumps(data, indent=2))

    def _load(self, session_id: str) -> Session | None:
        """Load a session by its session_id."""
        path = self._session_path(session_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
            session = Session(
                sender_id=data["sender_id"],
                session_id=data.get("session_id", session_id),
                title=data.get("title", ""),
                messages=data.get("messages", []),
                created_at=data.get("created_at", time.time()),
                last_active=data.get("last_active", time.time()),
                summary=data.get("summary", ""),
                token_count=data.get("token_count", 0),
            )
            if len(session.messages) > self._max_history:
                session.messages = self._trim_preserving_tool_pairs(
                    session.messages, self._max_history,
                )
            return session
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Corrupt session file %s, ignoring: %s", session_id, e)
            return None

    def load_session(self, session_id: str) -> Session | None:
        """Load a session by its session_id (returns None if not found)."""
        return self._load(session_id)

    def get_active_session_id(self, sender_id: str) -> str | None:
        """Get the active session ID for a sender, or None."""
        return self._load_active_index().get(sender_id)

    def session_stats(self) -> dict[str, int]:
        """Return stored session count and active sender count."""
        session_files = [
            p for p in self._dir.glob("*.json") if p.name != _ACTIVE_INDEX_FILE
        ]
        active_senders = len(self._load_active_index())
        return {"stored": len(session_files), "active_senders": active_senders}

    def _session_path(self, session_id: str) -> Path:
        """Get the filesystem path for a session file.

        Raises ValueError if the session_id contains path traversal characters.
        """
        if not re.fullmatch(r"[a-f0-9]+", session_id):
            raise ValueError(f"Invalid session_id: {session_id}")
        return self._dir / f"{session_id}.json"

    # -- active index --

    def _load_active_index(self) -> dict[str, str]:
        """Read the sender_id → session_id mapping."""
        path = self._dir / _ACTIVE_INDEX_FILE
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_active_index(self, index: dict[str, str]) -> None:
        """Write the sender_id → session_id mapping."""
        path = self._dir / _ACTIVE_INDEX_FILE
        path.write_text(json.dumps(index, indent=2))

    def _get_active_session_id(self, sender_id: str) -> str | None:
        """Get the active session ID for a sender, or None."""
        return self._load_active_index().get(sender_id)

    def _set_active_session_id(self, sender_id: str, session_id: str) -> None:
        """Set the active session ID for a sender."""
        index = self._load_active_index()
        index[sender_id] = session_id
        self._save_active_index(index)


def _sanitize_sender_id(sender_id: str) -> str:
    """Sanitize a sender ID for use as a filename.

    Phone numbers -> digits only. Other IDs -> alphanumeric + underscore.
    """
    # Strip everything except alphanumeric and underscore
    sanitized = re.sub(r"[^\w]", "", sender_id)
    return sanitized or "unknown"
