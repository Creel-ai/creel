"""Tests for the SessionStore interface and FileSessionStore implementation."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from creel.session import (
    FileSessionStore,
    Session,
    SessionCorruptedError,
    SessionFilter,
    SessionManager,
    SessionNotFoundError,
    SessionStore,
    SessionSummary,
)


class TestExceptions:
    """Exception hierarchy tests."""

    def test_session_not_found_is_value_error(self):
        """SessionNotFoundError should be a ValueError subclass for backward compat."""
        assert issubclass(SessionNotFoundError, ValueError)
        with pytest.raises(ValueError):
            raise SessionNotFoundError("gone")

    def test_session_corrupted_is_exception(self):
        assert issubclass(SessionCorruptedError, Exception)


class TestFileSessionStoreSaveLoad:
    """Core save / load round-trip."""

    def test_save_and_load(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        session = Session(sender_id="cli", total_tokens=42)
        session.messages.append({"role": "user", "content": "hi"})
        store.save(session)

        loaded = store.load(session.session_id)
        assert loaded.sender_id == "cli"
        assert loaded.session_id == session.session_id
        assert loaded.messages == [{"role": "user", "content": "hi"}]
        assert loaded.total_tokens == 42

    def test_save_creates_backup(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        session = Session(sender_id="cli")
        store.save(session)

        # Modify and save again — backup should appear
        session.messages.append({"role": "user", "content": "hello"})
        store.save(session)

        bak = tmp_path / f"{session.session_id}.bak"
        assert bak.exists()
        # Backup should contain old data (no messages)
        old_data = json.loads(bak.read_text())
        assert old_data["messages"] == []

    def test_atomic_save_no_partial_files(self, tmp_path: Path):
        """After save, only .json and possibly .bak should exist — no .tmp."""
        store = FileSessionStore(sessions_dir=str(tmp_path))
        session = Session(sender_id="cli")
        store.save(session)

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_save_persists_metadata_fields(self, tmp_path: Path):
        """JSON should contain updated_at, message_count, total_tokens."""
        store = FileSessionStore(sessions_dir=str(tmp_path))
        session = Session(sender_id="cli", total_tokens=100)
        session.messages = [{"role": "user", "content": "hi"}]
        store.save(session)

        raw = json.loads((tmp_path / f"{session.session_id}.json").read_text())
        assert raw["updated_at"] == session.last_active
        assert raw["message_count"] == 1
        assert raw["total_tokens"] == 100


class TestFileSessionStoreLoad:
    """Load edge cases."""

    def test_load_not_found(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        with pytest.raises(SessionNotFoundError):
            store.load("deadbeef00")

    def test_load_corrupted(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        (tmp_path / "badcafe0.json").write_text("not json{{{")
        with pytest.raises(SessionCorruptedError):
            store.load("badcafe0")

    def test_load_migration_old_format(self, tmp_path: Path):
        """Old session files without total_tokens/updated_at load with defaults."""
        old_data = {
            "sender_id": "cli",
            "session_id": "aabbccdd",
            "title": "Old session",
            "created_at": 1000000.0,
            "last_active": 1000001.0,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        (tmp_path / "aabbccdd.json").write_text(json.dumps(old_data))

        store = FileSessionStore(sessions_dir=str(tmp_path))
        session = store.load("aabbccdd")
        assert session.total_tokens == 0
        assert session.token_count == 0
        assert session.summary == ""


class TestFileSessionStoreDelete:
    def test_delete_removes_file(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        session = Session(sender_id="cli")
        store.save(session)
        # Save twice to create .bak
        store.save(session)

        store.delete(session.session_id)
        assert not (tmp_path / f"{session.session_id}.json").exists()
        assert not (tmp_path / f"{session.session_id}.bak").exists()

    def test_delete_not_found(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        with pytest.raises(SessionNotFoundError):
            store.delete("deadbeef00")


class TestFileSessionStoreEncryption:
    """Encrypted save / load round-trip."""

    @staticmethod
    def _fernet_key() -> str:
        """Generate a valid Fernet key (44-char base64)."""
        return base64.urlsafe_b64encode(b"\x00" * 32).decode()

    def test_encrypted_round_trip(self, tmp_path: Path):
        key = self._fernet_key()
        store = FileSessionStore(sessions_dir=str(tmp_path), encryption_key=key)
        session = Session(sender_id="cli", total_tokens=99)
        session.messages.append({"role": "user", "content": "secret"})
        store.save(session)

        # Raw file should NOT be valid JSON (it's encrypted)
        raw = (tmp_path / f"{session.session_id}.json").read_bytes()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

        loaded = store.load(session.session_id)
        assert loaded.sender_id == "cli"
        assert loaded.messages == [{"role": "user", "content": "secret"}]
        assert loaded.total_tokens == 99

    def test_passphrase_round_trip(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path), encryption_key="my-passphrase")
        session = Session(sender_id="cli")
        session.messages.append({"role": "user", "content": "hidden"})
        store.save(session)

        loaded = store.load(session.session_id)
        assert loaded.messages == [{"role": "user", "content": "hidden"}]


class TestFileSessionStoreList:
    def test_list_returns_summaries(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        s1 = Session(sender_id="cli", total_tokens=10)
        s1.messages = [{"role": "user", "content": "hi"}]
        store.save(s1)

        s2 = Session(sender_id="cli", total_tokens=20)
        s2.messages = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
        ]
        store.save(s2)

        results = store.list()
        assert len(results) == 2
        assert all(isinstance(r, SessionSummary) for r in results)

    def test_list_with_sender_filter(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        store.save(Session(sender_id="alice"))
        store.save(Session(sender_id="bob"))

        alice_sessions = store.list(SessionFilter(sender_id="alice"))
        assert len(alice_sessions) == 1
        assert alice_sessions[0].sender_id == "alice"

    def test_list_with_time_filter(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        old = Session(sender_id="cli")
        old.created_at = 1000.0
        old.last_active = 1000.0
        store.save(old)

        new = Session(sender_id="cli")
        new.created_at = 9999.0
        new.last_active = 9999.0
        store.save(new)

        recent = store.list(SessionFilter(created_after=5000.0))
        assert len(recent) == 1
        assert recent[0].session_id == new.session_id

    def test_list_sorted_by_updated_at_desc(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        older = Session(sender_id="cli")
        older.last_active = 100.0
        store.save(older)

        newer = Session(sender_id="cli")
        newer.last_active = 200.0
        store.save(newer)

        results = store.list()
        assert results[0].session_id == newer.session_id
        assert results[1].session_id == older.session_id

    def test_list_skips_corrupt_files(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        store.save(Session(sender_id="cli"))
        (tmp_path / "badfile.json").write_text("corrupt{{{")

        results = store.list()
        assert len(results) == 1

    def test_list_skips_active_index_file(self, tmp_path: Path):
        """The _active.json index file should not appear in list results."""
        store = FileSessionStore(sessions_dir=str(tmp_path))
        store.save(Session(sender_id="cli"))
        # Simulate the active index file that SessionManager creates
        (tmp_path / "_active.json").write_text(json.dumps({"cli": "abc123"}))

        results = store.list()
        assert len(results) == 1
        assert results[0].sender_id == "cli"


class TestFileSessionStoreExists:
    def test_exists_true(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        session = Session(sender_id="cli")
        store.save(session)
        assert store.exists(session.session_id) is True

    def test_exists_false(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        assert store.exists("deadbeef00") is False

    def test_exists_invalid_id(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        assert store.exists("../etc/passwd") is False


class TestSessionStoreIsABC:
    """Verify SessionStore is a proper abstract base class."""

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            SessionStore()  # type: ignore[abstract]

    def test_file_store_is_subclass(self):
        assert issubclass(FileSessionStore, SessionStore)


class TestSessionManagerWithStore:
    """SessionManager should delegate to the provided store."""

    def test_custom_store_used(self, tmp_path: Path):
        store = FileSessionStore(sessions_dir=str(tmp_path))
        mgr = SessionManager(sessions_dir=str(tmp_path), store=store)

        session = mgr.add_user_message("cli", "Hello")
        assert store.exists(session.session_id)

    def test_total_tokens_accumulated(self, tmp_path: Path):
        """update_token_count should accumulate total_tokens."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        mgr.add_user_message("cli", "Hello")
        mgr.add_assistant_response("cli", [{"type": "text", "text": "Hi"}])
        mgr.add_user_message("cli", "More")

        mgr.update_token_count("cli", 500)
        session = mgr.get_or_create("cli")
        assert session.total_tokens == 500

        mgr.update_token_count("cli", 600)
        session = mgr.get_or_create("cli")
        assert session.total_tokens == 1100

    def test_list_sessions_includes_total_tokens(self, tmp_path: Path):
        """list_sessions dicts should include total_tokens."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        mgr.add_user_message("cli", "Hello")

        sessions = mgr.list_sessions("cli")
        assert len(sessions) == 1
        assert "total_tokens" in sessions[0]

    def test_resume_raises_session_not_found(self, tmp_path: Path):
        """resume_session should raise SessionNotFoundError (a ValueError)."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        mgr.add_user_message("cli", "Hello")

        with pytest.raises(SessionNotFoundError):
            mgr.resume_session("cli", "deadbeef00")

        # Also catchable as ValueError
        with pytest.raises(ValueError):
            mgr.resume_session("cli", "deadbeef00")
