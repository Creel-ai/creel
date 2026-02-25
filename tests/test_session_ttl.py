"""Tests for session TTL / auto-expiry."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from creel.session import Session, SessionManager


class TestSessionTTL:
    """Tests for TTL-based session expiry."""

    def test_no_ttl_sessions_never_expire(self, tmp_path: Path):
        mgr = SessionManager(sessions_dir=str(tmp_path), ttl_hours=0)
        s = mgr.add_user_message("cli", "Hello")
        # Manually set last_active to way in the past
        s.last_active = time.time() - 999999
        mgr._save(s)
        loaded = mgr.get_or_create("cli")
        assert loaded.session_id == s.session_id

    def test_expired_session_creates_new(self, tmp_path: Path):
        mgr = SessionManager(sessions_dir=str(tmp_path), ttl_hours=1)
        s = mgr.add_user_message("cli", "Hello")
        old_id = s.session_id
        # Set last_active to 2 hours ago
        s.last_active = time.time() - 7200
        mgr._save(s)
        new_session = mgr.get_or_create("cli")
        assert new_session.session_id != old_id
        assert new_session.messages == []

    def test_non_expired_session_reused(self, tmp_path: Path):
        mgr = SessionManager(sessions_dir=str(tmp_path), ttl_hours=24)
        s = mgr.add_user_message("cli", "Hello")
        loaded = mgr.get_or_create("cli")
        assert loaded.session_id == s.session_id

    def test_cleanup_expired_removes_old(self, tmp_path: Path):
        mgr = SessionManager(sessions_dir=str(tmp_path), ttl_hours=1)
        s = mgr.add_user_message("cli", "Hello")
        s.last_active = time.time() - 7200
        mgr._save(s)
        removed = mgr.cleanup_expired("cli")
        assert removed == 1
        # File should be gone
        assert not (tmp_path / f"{s.session_id}.json").exists()

    def test_cleanup_expired_keeps_recent(self, tmp_path: Path):
        mgr = SessionManager(sessions_dir=str(tmp_path), ttl_hours=1)
        s = mgr.add_user_message("cli", "Hello")
        removed = mgr.cleanup_expired("cli")
        assert removed == 0
        assert (tmp_path / f"{s.session_id}.json").exists()

    def test_cleanup_no_ttl_returns_zero(self, tmp_path: Path):
        mgr = SessionManager(sessions_dir=str(tmp_path), ttl_hours=0)
        mgr.add_user_message("cli", "Hello")
        assert mgr.cleanup_expired("cli") == 0

    def test_ttl_config_in_model(self):
        from creel.models import SessionConfig
        cfg = SessionConfig(ttl_hours=48)
        assert cfg.ttl_hours == 48
        cfg2 = SessionConfig()
        assert cfg2.ttl_hours == 0
