"""Tests for session security hardening (ID entropy + encryption at rest)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskrunner.session import Session, SessionManager


class TestSessionIDEntropy:
    """Tests for increased session ID entropy (128-bit)."""

    def test_session_id_length(self) -> None:
        """Session ID should be 32 hex chars (128-bit / 16 bytes)."""
        session = Session(sender_id="test")
        assert len(session.session_id) == 32

    def test_session_id_hex(self) -> None:
        """Session ID should be valid hex."""
        session = Session(sender_id="test")
        int(session.session_id, 16)  # Should not raise

    def test_session_id_unique(self) -> None:
        """Two sessions should have different IDs."""
        s1 = Session(sender_id="test")
        s2 = Session(sender_id="test")
        assert s1.session_id != s2.session_id

    def test_session_id_in_path(self, tmp_path: Path) -> None:
        """New session ID format should work with filesystem paths."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session = mgr.get_or_create("cli")
        assert len(session.session_id) == 32
        # Should be persisted
        files = [f for f in tmp_path.glob("*.json") if f.name != "_active.json"]
        # The session is lazily saved, so save it first
        mgr._save(session)
        files = [f for f in tmp_path.glob("*.json") if f.name != "_active.json"]
        assert len(files) == 1
        assert files[0].stem == session.session_id


class TestSessionEncryption:
    """Tests for session encryption at rest."""

    @pytest.fixture
    def encryption_key(self) -> str:
        """A test passphrase for encryption."""
        return "test-encryption-passphrase-12345"

    def test_encrypted_session_roundtrip(self, tmp_path: Path, encryption_key: str) -> None:
        """Sessions should be encrypted on save and decrypted on load."""
        pytest.importorskip("cryptography")

        mgr = SessionManager(
            sessions_dir=str(tmp_path),
            encryption_key=encryption_key,
        )
        session = mgr.add_user_message("cli", "Secret message")

        # File should not contain plaintext
        files = [f for f in tmp_path.glob("*.json") if f.name != "_active.json"]
        assert len(files) == 1
        raw = files[0].read_bytes()
        assert b"Secret message" not in raw

        # Should load correctly
        loaded = mgr.get_or_create("cli")
        assert loaded.messages[0]["content"] == "Secret message"

    def test_encrypted_session_not_readable_as_json(
        self, tmp_path: Path, encryption_key: str
    ) -> None:
        """Encrypted sessions should not be valid JSON."""
        pytest.importorskip("cryptography")

        mgr = SessionManager(
            sessions_dir=str(tmp_path),
            encryption_key=encryption_key,
        )
        mgr.add_user_message("cli", "Test message")

        files = [f for f in tmp_path.glob("*.json") if f.name != "_active.json"]
        raw = files[0].read_text(errors="replace")
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_unencrypted_fallback(self, tmp_path: Path, encryption_key: str) -> None:
        """Encrypted manager should fall back to reading unencrypted files."""
        pytest.importorskip("cryptography")

        # First create an unencrypted session
        mgr_plain = SessionManager(sessions_dir=str(tmp_path))
        session = mgr_plain.add_user_message("cli", "Plain text")
        sid = session.session_id

        # Now create an encrypted manager and try to read it
        mgr_enc = SessionManager(
            sessions_dir=str(tmp_path),
            encryption_key=encryption_key,
        )
        loaded = mgr_enc._load(sid)
        assert loaded is not None
        assert loaded.messages[0]["content"] == "Plain text"

    def test_no_encryption_by_default(self, tmp_path: Path) -> None:
        """Without encryption_key, sessions should be stored as plain JSON."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session = mgr.add_user_message("cli", "Visible message")

        files = [f for f in tmp_path.glob("*.json") if f.name != "_active.json"]
        raw = files[0].read_text()
        data = json.loads(raw)
        assert data["messages"][0]["content"] == "Visible message"

    def test_list_sessions_with_encryption(
        self, tmp_path: Path, encryption_key: str
    ) -> None:
        """list_sessions should work with encrypted files."""
        pytest.importorskip("cryptography")

        mgr = SessionManager(
            sessions_dir=str(tmp_path),
            encryption_key=encryption_key,
        )
        mgr.add_user_message("cli", "Session 1")
        s2 = mgr.new_session("cli")
        mgr.add_user_message("cli", "Session 2")

        sessions = mgr.list_sessions("cli")
        assert len(sessions) == 2

    def test_wrong_key_falls_back_to_plaintext(self, tmp_path: Path) -> None:
        """Wrong encryption key should fall back to plaintext if available."""
        pytest.importorskip("cryptography")

        # Create a plaintext session
        mgr_plain = SessionManager(sessions_dir=str(tmp_path))
        session = mgr_plain.add_user_message("cli", "test")
        sid = session.session_id

        # Try with wrong key — should still read the plaintext file
        mgr_wrong = SessionManager(
            sessions_dir=str(tmp_path),
            encryption_key="wrong-key",
        )
        loaded = mgr_wrong._load(sid)
        assert loaded is not None
        assert loaded.messages[0]["content"] == "test"


class TestSessionConfigEncryption:
    """Tests for encryption_key in SessionConfig."""

    def test_session_config_encryption_key_default(self) -> None:
        from taskrunner.models import SessionConfig

        config = SessionConfig()
        assert config.encryption_key is None

    def test_session_config_encryption_key_set(self) -> None:
        from taskrunner.models import SessionConfig

        config = SessionConfig(encryption_key="my-secret")
        assert config.encryption_key == "my-secret"
