"""Session manager - persistent conversation sessions backed by JSON files.

Provides a clean SessionStore interface for storage backends with a default
file-based implementation (FileSessionStore) that supports atomic saves,
backup, and optional encryption at rest.

Storage backend abstraction enables future pluggable backends (SQLite, Redis)
by implementing the SessionStore interface.

Migration: Existing session files without newer fields (total_tokens,
updated_at, message_count) are loaded with sensible defaults and upgraded
on next save.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Salt length prepended to encrypted session files
_SALT_LENGTH = 16
# PBKDF2 iterations — OWASP 2024 recommendation for SHA-256
_KDF_ITERATIONS = 600_000
# Advisory file lock timeout in seconds
_LOCK_TIMEOUT = 5


class SessionLockError(OSError):
    """Raised when a session file lock cannot be acquired within the timeout."""


# -- exceptions --


class SessionNotFoundError(ValueError):
    """Raised when a requested session does not exist.

    Inherits from ValueError for backward compatibility with code that
    previously caught ValueError from resume_session / load_session.
    """


class SessionCorruptedError(Exception):
    """Raised when a session file exists but cannot be parsed or decrypted."""


# -- encryption helpers --


def _derive_fernet_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a Fernet-compatible key from a passphrase using PBKDF2.

    Uses PBKDF2-HMAC-SHA256 with a random salt and 600k iterations.
    Returns a 32-byte URL-safe base64-encoded key suitable for Fernet.
    """
    raw = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, _KDF_ITERATIONS)
    return base64.urlsafe_b64encode(raw)


def _encrypt_data(data: bytes, key: bytes) -> bytes:
    """Encrypt data using Fernet symmetric encryption."""
    from cryptography.fernet import Fernet

    f = Fernet(key)
    return f.encrypt(data)


def _decrypt_data(token: bytes, key: bytes) -> bytes:
    """Decrypt Fernet-encrypted data."""
    from cryptography.fernet import Fernet

    f = Fernet(key)
    return f.decrypt(token)


def _flock_with_timeout(fd: int, operation: int, timeout: float = _LOCK_TIMEOUT) -> None:
    """Acquire an advisory file lock with a timeout.

    Uses non-blocking flock in a polling loop.  Raises ``SessionLockError``
    if the lock cannot be acquired within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SessionLockError(
                    f"Could not acquire file lock within {timeout}s. "
                    "Another process may be writing to this session."
                ) from None
            time.sleep(0.05)


_ACTIVE_INDEX_FILE = "_active.json"


# -- data classes --


@dataclass
class SessionFilter:
    """Filter criteria for listing sessions."""

    sender_id: str | None = None
    created_after: float | None = None
    created_before: float | None = None


@dataclass
class SessionSummary:
    """Lightweight session metadata returned by list operations."""

    session_id: str
    sender_id: str
    title: str
    created_at: float
    updated_at: float
    message_count: int
    total_tokens: int


@dataclass
class Session:
    """A conversation session with message history."""

    sender_id: str
    session_id: str = field(default_factory=lambda: secrets.token_hex(16))
    title: str = ""
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    summary: str = ""
    token_count: int = 0
    total_tokens: int = 0


# -- storage backend abstraction --


class SessionStore(ABC):
    """Abstract interface for session persistence backends.

    Implementations handle the raw storage of session data.  The default
    FileSessionStore writes JSON files with atomic saves and optional
    encryption.  Future backends (SQLite, Redis) can implement this
    interface for pluggable storage.
    """

    @abstractmethod
    def save(self, session: Session) -> None:
        """Persist a session.  Creates a backup of any existing data first."""

    @abstractmethod
    def load(self, session_id: str) -> Session:
        """Load a session by ID.

        Raises:
            SessionNotFoundError: If the session does not exist.
            SessionCorruptedError: If the session data is corrupt.
        """

    @abstractmethod
    def delete(self, session_id: str) -> None:
        """Delete a session by ID.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """

    @abstractmethod
    def list(self, session_filter: SessionFilter | None = None) -> list[SessionSummary]:
        """List sessions matching the optional filter criteria."""

    @abstractmethod
    def exists(self, session_id: str) -> bool:
        """Check whether a session exists."""


class FileSessionStore(SessionStore):
    """File-based session store with atomic saves and optional encryption.

    Sessions are stored as individual JSON files.  Writes use atomic
    temp-file-then-rename to prevent corruption from interrupted writes.
    A ``.bak`` copy of the previous version is kept before each save.
    """

    def __init__(
        self,
        sessions_dir: str = "sessions",
        encryption_key: str | None = None,
    ):
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._encryption_key: bytes | None = None
        self._encryption_passphrase: str | None = None

        if encryption_key:
            try:
                key_bytes = encryption_key.encode()
                base64.urlsafe_b64decode(key_bytes)
                if len(key_bytes) == 44:
                    self._encryption_key = key_bytes
                else:
                    self._encryption_passphrase = encryption_key
            except Exception:
                logger.debug("Encryption key is not valid base64, treating as passphrase")
                self._encryption_passphrase = encryption_key
            logger.info("Session encryption enabled")

    @property
    def dir(self) -> Path:
        """The directory where session files are stored."""
        return self._dir

    def save(self, session: Session) -> None:
        """Persist a session atomically with backup."""
        data = {
            "session_id": session.session_id,
            "sender_id": session.sender_id,
            "title": session.title,
            "created_at": session.created_at,
            "last_active": session.last_active,
            "updated_at": session.last_active,
            "messages": session.messages,
            "summary": session.summary,
            "token_count": session.token_count,
            "total_tokens": session.total_tokens,
            "message_count": len(session.messages),
        }

        path = self._session_path(session.session_id)
        json_bytes = json.dumps(data, indent=2).encode()

        # Create backup of existing file
        if path.exists():
            bak_path = path.with_suffix(".bak")
            try:
                bak_path.write_bytes(path.read_bytes())
            except OSError:
                logger.debug("Could not create backup for %s", path.name, exc_info=True)

        # Atomic write: temp file → fsync → rename
        content = self._maybe_encrypt(json_bytes)
        fd = None
        tmp_path_str = None
        try:
            fd, tmp_path_str = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            os.write(fd, content)
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(tmp_path_str, path)
        except BaseException:
            if fd is not None:
                os.close(fd)
            if tmp_path_str is not None:
                try:
                    os.unlink(tmp_path_str)
                except OSError:
                    pass
            raise

    def load(self, session_id: str) -> Session:
        """Load a session from a JSON file.

        Raises:
            SessionNotFoundError: If the session file does not exist.
            SessionCorruptedError: If the file cannot be parsed.
        """
        path = self._session_path(session_id)
        if not path.exists():
            raise SessionNotFoundError(f"Session {session_id} not found")

        try:
            raw_bytes = path.read_bytes()
            data = self._decrypt_or_parse(raw_bytes)
            return Session(
                sender_id=data["sender_id"],
                session_id=data.get("session_id", session_id),
                title=data.get("title", ""),
                messages=data.get("messages", []),
                created_at=data.get("created_at", time.time()),
                last_active=data.get("last_active", time.time()),
                summary=data.get("summary", ""),
                token_count=data.get("token_count", 0),
                total_tokens=data.get("total_tokens", 0),
            )
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
            raise SessionCorruptedError(f"Session {session_id} is corrupted: {e}") from e

    def delete(self, session_id: str) -> None:
        """Delete a session file and its backup."""
        path = self._session_path(session_id)
        if not path.exists():
            raise SessionNotFoundError(f"Session {session_id} not found")
        path.unlink()
        bak_path = path.with_suffix(".bak")
        if bak_path.exists():
            bak_path.unlink()

    def list(self, session_filter: SessionFilter | None = None) -> list[SessionSummary]:
        """List sessions matching filter criteria."""
        results: list[SessionSummary] = []
        for path in self._dir.glob("*.json"):
            if path.name == _ACTIVE_INDEX_FILE:
                continue
            try:
                raw_bytes = path.read_bytes()
                data = self._decrypt_or_parse(raw_bytes)
            except Exception:
                logger.debug("Could not read session file %s, skipping", path.name, exc_info=True)
                continue

            # Apply filters
            if session_filter:
                if session_filter.sender_id and data.get("sender_id") != session_filter.sender_id:
                    continue
                created = data.get("created_at", 0)
                if session_filter.created_after and created < session_filter.created_after:
                    continue
                if session_filter.created_before and created > session_filter.created_before:
                    continue

            results.append(
                SessionSummary(
                    session_id=data.get("session_id", path.stem),
                    sender_id=data.get("sender_id", ""),
                    title=data.get("title", ""),
                    created_at=data.get("created_at", 0),
                    updated_at=data.get("last_active", 0),
                    message_count=data.get("message_count", len(data.get("messages", []))),
                    total_tokens=data.get("total_tokens", 0),
                )
            )

        results.sort(key=lambda r: r.updated_at, reverse=True)
        return results

    def exists(self, session_id: str) -> bool:
        """Check if a session file exists."""
        try:
            path = self._session_path(session_id)
        except ValueError:
            return False
        return path.exists()

    # -- internal helpers --

    def _session_path(self, session_id: str) -> Path:
        """Get the filesystem path for a session file.

        Raises ValueError if the session_id contains path traversal characters.
        """
        if not re.fullmatch(r"[a-f0-9]+", session_id):
            raise ValueError(f"Invalid session_id: {session_id}")
        return self._dir / f"{session_id}.json"

    def _maybe_encrypt(self, json_bytes: bytes) -> bytes:
        """Encrypt data if encryption is configured, otherwise return as-is."""
        if self._encryption_key:
            return _encrypt_data(json_bytes, self._encryption_key)
        if self._encryption_passphrase:
            salt = os.urandom(_SALT_LENGTH)
            key = _derive_fernet_key(self._encryption_passphrase, salt)
            return salt + _encrypt_data(json_bytes, key)
        return json_bytes

    def _decrypt_or_parse(self, raw_bytes: bytes) -> dict:
        """Decrypt (if encryption enabled) or parse raw session bytes.

        Tries decryption first, falls back to plaintext for backward compat.
        """
        if self._encryption_key:
            try:
                decrypted = _decrypt_data(raw_bytes, self._encryption_key)
                return json.loads(decrypted)
            except Exception:
                logger.warning(
                    "Decryption failed, falling back to plaintext (may be an old unencrypted file)",
                    exc_info=True,
                )
                return json.loads(raw_bytes)
        if self._encryption_passphrase:
            try:
                salt = raw_bytes[:_SALT_LENGTH]
                key = _derive_fernet_key(self._encryption_passphrase, salt)
                decrypted = _decrypt_data(raw_bytes[_SALT_LENGTH:], key)
                return json.loads(decrypted)
            except Exception:
                logger.warning(
                    "Passphrase decryption failed, falling back to plaintext "
                    "(may be an old unencrypted file)",
                    exc_info=True,
                )
                return json.loads(raw_bytes)
        return json.loads(raw_bytes)


class SessionManager:
    """Manages conversation sessions with persistence and compaction.

    Delegates raw storage to a :class:`SessionStore` backend (default:
    :class:`FileSessionStore`).  Handles higher-level concerns: active
    session tracking, compaction with summarization, and TTL management.

    Context window management is handled by the context pruner in the
    agent loop, not by message trimming on save/load.
    """

    def __init__(
        self,
        sessions_dir: str = "sessions",
        ttl_hours: float = 0,
        summarize_on_trim: bool = False,
        summarize_fn: Callable[[list[dict]], str] | None = None,
        max_context_tokens: int = 180_000,
        encryption_key: str | None = None,
        on_session_archived: Callable[[str, list[dict]], None] | None = None,
        store: SessionStore | None = None,
    ):
        if store is not None:
            self._store = store
            self._dir = Path(sessions_dir)
            self._dir.mkdir(parents=True, exist_ok=True)
        else:
            file_store = FileSessionStore(
                sessions_dir=sessions_dir,
                encryption_key=encryption_key,
            )
            self._store = file_store
            self._dir = file_store.dir

        self._ttl_seconds = ttl_hours * 3600 if ttl_hours > 0 else 0
        self._summarize_fn = summarize_fn
        self._max_context_tokens = max_context_tokens
        self._on_session_archived = on_session_archived

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
                        session.session_id,
                        session.last_active,
                    )
                else:
                    return session

        # No active session (or file missing or expired) — create fresh
        session = Session(sender_id=sender_id)
        self._set_active_session_id(sender_id, session.session_id)
        self._save(session)
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
        summaries = self._store.list(SessionFilter(sender_id=sender_id))
        for s in summaries:
            if (time.time() - s.updated_at) > self._ttl_seconds:
                try:
                    self._store.delete(s.session_id)
                    removed += 1
                    logger.info("Removed expired session %s", s.session_id)
                except SessionNotFoundError:
                    pass
        return removed

    def new_session(self, sender_id: str) -> Session:
        """Save the current session and start a fresh one."""
        # Save any existing active session first
        active_id = self._get_active_session_id(sender_id)
        if active_id:
            existing = self._load(active_id)
            if existing is not None:
                # Archive transcript before starting fresh
                if self._on_session_archived and existing.messages:
                    try:
                        self._on_session_archived(existing.session_id, list(existing.messages))
                    except (OSError, RuntimeError, ValueError):
                        logger.warning("on_session_archived failed", exc_info=True)
                self._save(existing)

        session = Session(sender_id=sender_id)
        self._save(session)
        self._set_active_session_id(sender_id, session.session_id)
        logger.info("Started new session %s for %s", session.session_id, sender_id)
        return session

    def list_sessions(self, sender_id: str) -> list[dict]:
        """Return metadata for all sessions belonging to a sender, sorted by last_active desc."""
        summaries = self._store.list(SessionFilter(sender_id=sender_id))
        return [
            {
                "session_id": s.session_id,
                "title": s.title,
                "created_at": s.created_at,
                "last_active": s.updated_at,
                "message_count": s.message_count,
                "total_tokens": s.total_tokens,
            }
            for s in summaries
        ]

    def resume_session(self, sender_id: str, session_id: str) -> Session:
        """Switch the active session to an existing one.

        Raises SessionNotFoundError if the session doesn't exist or belongs
        to another sender.
        """
        try:
            session = self._store.load(session_id)
        except (SessionNotFoundError, SessionCorruptedError) as exc:
            raise SessionNotFoundError(f"Session {session_id} not found") from exc
        if session.sender_id != sender_id:
            raise SessionNotFoundError(f"Session {session_id} not found")
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

    def add_user_message_blocks(self, sender_id: str, content_blocks: list[dict]) -> Session:
        """Add a user message with content blocks (e.g. text + images).

        Used when media attachments produce multi-modal content for the LLM.
        """
        session = self.get_or_create(sender_id)
        session.messages.append({"role": "user", "content": content_blocks})
        session.last_active = time.time()
        # Set title from the first text block
        if not session.title:
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    session.title = block["text"][:60].strip()
                    break
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
                # Archive transcript before clearing
                if self._on_session_archived and session.messages:
                    try:
                        self._on_session_archived(session.session_id, list(session.messages))
                    except (OSError, RuntimeError, ValueError):
                        logger.warning("on_session_archived failed", exc_info=True)
                session.messages = []
                session.title = ""
                self._save(session)
                logger.info("Cleared session %s for %s", active_id, sender_id)
                return
        logger.info("No active session to clear for %s", sender_id)

    # -- compaction --

    def compact(self, sender_id: str) -> None:
        """Explicitly compact a session, summarizing older messages.

        Called by the ``/compact`` command. Requires ``summarize_fn`` to be
        configured; falls back to simple trimming otherwise.
        """
        session = self.get_or_create(sender_id)
        if len(session.messages) <= 2:
            return
        self._compact_with_summary(session)
        self._save(session)

    def update_token_count(self, sender_id: str, input_tokens: int) -> None:
        """Record token usage for a session.

        Called once per LLM turn with the input_tokens from usage data.
        Persists to disk so that total_tokens metadata stays accurate
        across restarts.
        """
        session = self.get_or_create(sender_id)
        session.token_count = input_tokens
        session.total_tokens += input_tokens
        self._save(session)

    def _compact_with_summary(self, session: Session) -> None:
        """Compact older messages into a summary, keeping recent messages."""
        if not self._summarize_fn:
            logger.warning("Compaction requested but no summarize_fn configured; skipping")
            return

        keep_count = len(session.messages) // 2
        split_idx = self._find_safe_split(session.messages, len(session.messages) - keep_count)
        older = session.messages[:split_idx]
        recent = session.messages[split_idx:]

        if not older:
            return

        # Archive older messages before they're discarded
        if self._on_session_archived:
            try:
                self._on_session_archived(session.session_id, list(older))
            except (OSError, RuntimeError, ValueError):
                logger.warning("on_session_archived failed during compaction", exc_info=True)

        try:
            summary_text = self._summarize_fn(older)
        except Exception:
            logger.warning("Summarization failed; messages unchanged", exc_info=True)
            return

        summary_msg = {
            "role": "user",
            "content": (f"[CONVERSATION SUMMARY]\n<summary>\n{summary_text}\n</summary>"),
        }

        session.messages = [summary_msg] + recent
        session.summary = summary_text
        session.token_count = 0
        logger.info(
            "Compacted session %s: %d messages -> 1 summary + %d recent",
            session.session_id,
            len(older) + len(recent),
            len(recent),
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

    # -- persistence (delegates to store) --

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
        """Write session via store."""
        self._store.save(session)

    def _load(self, session_id: str) -> Session | None:
        """Load a session, returning None on missing or corrupt."""
        try:
            return self._store.load(session_id)
        except SessionNotFoundError:
            return None
        except SessionCorruptedError:
            logger.warning("Corrupt session file %s, ignoring", session_id)
            return None

    def load_session(self, session_id: str) -> Session | None:
        """Load a session by its session_id (returns None if not found)."""
        return self._load(session_id)

    def get_active_session_id(self, sender_id: str) -> str | None:
        """Get the active session ID for a sender, or None."""
        return self._load_active_index().get(sender_id)

    def session_stats(self) -> dict[str, int]:
        """Return stored session count and active sender count."""
        session_files = [p for p in self._dir.glob("*.json") if p.name != _ACTIVE_INDEX_FILE]
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

    @staticmethod
    def _locked_read(path: Path) -> bytes:
        """Read a file while holding a shared advisory lock."""
        with open(path, "rb") as f:
            _flock_with_timeout(f.fileno(), fcntl.LOCK_SH)
            return f.read()

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        """Write *payload* to *path* atomically with advisory locking.

        1. Write to a temp file in the same directory (same filesystem).
        2. Acquire an exclusive advisory lock on a ``.lock`` sentinel file
           shared by all writers to the same *path* (serializes writers).
        3. Copy the existing file to ``<path>.bak`` (if present).
        4. ``os.replace`` (atomic rename) temp → final path.
        """
        tmp_name: str | None = None
        try:
            fd_tmp, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                os.write(fd_tmp, payload)
                os.fsync(fd_tmp)
            finally:
                os.close(fd_tmp)

            lock_path = path.with_suffix(path.suffix + ".lock")
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            try:
                _flock_with_timeout(lock_fd, fcntl.LOCK_EX)

                # Backup previous version (copy, not rename, so the original
                # stays in place until the final atomic replace below)
                if path.exists():
                    bak = path.with_suffix(".json.bak")
                    try:
                        shutil.copy2(str(path), str(bak))
                    except OSError:
                        logger.debug("Could not create backup for %s", path.name)

                os.replace(tmp_name, str(path))
                tmp_name = None  # rename succeeded, don't clean up
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _load_active_index(self) -> dict[str, str]:
        """Read the sender_id → session_id mapping with shared lock."""
        path = self._dir / _ACTIVE_INDEX_FILE
        if not path.exists():
            return {}
        try:
            return json.loads(self._locked_read(path))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_active_index(self, index: dict[str, str]) -> None:
        """Write the sender_id → session_id mapping with atomic write."""
        path = self._dir / _ACTIVE_INDEX_FILE
        self._atomic_write(path, json.dumps(index, indent=2).encode())

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
