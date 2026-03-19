"""Tests for temporary policy overrides (guardian.overrides)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from guardian.overrides import (
    TemporaryOverride,
    TemporaryOverrideManager,
    parse_duration,
    parse_use_count,
)
from guardian.types import ActionVerdict, OverrideConfig

# --- parse_duration ---


class TestParseDuration:
    def test_minutes(self):
        assert parse_duration("30m") == 1800

    def test_hours(self):
        assert parse_duration("2h") == 7200

    def test_seconds(self):
        assert parse_duration("90s") == 90

    def test_combined_hm(self):
        assert parse_duration("1h30m") == 5400

    def test_combined_hms(self):
        assert parse_duration("1h30m45s") == 5445

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Empty duration"):
            parse_duration("")

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("abc")

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            parse_duration("0m")

    def test_whitespace_stripped(self):
        assert parse_duration("  15m  ") == 900


# --- parse_use_count ---


class TestParseUseCount:
    def test_count_only(self):
        count, rest = parse_use_count("10x")
        assert count == 10
        assert rest == ""

    def test_count_with_duration(self):
        count, rest = parse_use_count("5x 30m")
        assert count == 5
        assert rest == "30m"

    def test_no_count(self):
        count, rest = parse_use_count("30m")
        assert count is None
        assert rest == "30m"

    def test_empty(self):
        count, rest = parse_use_count("")
        assert count is None
        assert rest == ""


# --- TemporaryOverride dataclass ---


class TestTemporaryOverride:
    def test_is_active(self):
        ov = TemporaryOverride(
            id="abc",
            pattern="weather",
            action=ActionVerdict.ALLOW,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            created_at=datetime.now(UTC),
            created_by="test",
        )
        assert ov.is_active
        assert not ov.is_expired
        assert not ov.is_exhausted

    def test_is_expired(self):
        ov = TemporaryOverride(
            id="abc",
            pattern="weather",
            action=ActionVerdict.ALLOW,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            created_at=datetime.now(UTC) - timedelta(minutes=30),
            created_by="test",
        )
        assert ov.is_expired
        assert not ov.is_active

    def test_is_exhausted(self):
        ov = TemporaryOverride(
            id="abc",
            pattern="weather",
            action=ActionVerdict.ALLOW,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            created_at=datetime.now(UTC),
            created_by="test",
            use_count=3,
            max_uses=3,
        )
        assert ov.is_exhausted
        assert not ov.is_active

    def test_remaining_seconds(self):
        ov = TemporaryOverride(
            id="abc",
            pattern="weather",
            action=ActionVerdict.ALLOW,
            expires_at=datetime.now(UTC) + timedelta(seconds=120),
            created_at=datetime.now(UTC),
            created_by="test",
        )
        assert 118 <= ov.remaining_seconds <= 120


# --- TemporaryOverrideManager ---


@pytest.fixture
def manager():
    config = OverrideConfig()
    return TemporaryOverrideManager(config)


@pytest.fixture
def manager_with_audit():
    config = OverrideConfig()
    audit = MagicMock()
    return TemporaryOverrideManager(config, audit=audit)


class TestOverrideManager:
    def test_create_and_check(self, manager):
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        decision = manager.check("weather", {})
        assert decision is not None
        assert decision.verdict == ActionVerdict.ALLOW
        assert "temp_override" in decision.matched_rule

    def test_no_match_returns_none(self, manager):
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        assert manager.check("gmail_send", {}) is None

    def test_glob_pattern(self, manager):
        manager.create_override(
            pattern="github.*",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        assert manager.check("github.create_issue", {}) is not None
        assert manager.check("github.list_repos", {}) is not None
        assert manager.check("gmail_send", {}) is None

    def test_revoke(self, manager):
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        revoked = manager.revoke_override("weather")
        assert revoked is not None
        assert manager.check("weather", {}) is None

    def test_revoke_not_found(self, manager):
        assert manager.revoke_override("nonexistent") is None

    def test_list_active(self, manager):
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        manager.create_override(
            pattern="gmail_send",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        active = manager.list_active()
        assert len(active) == 2

    def test_expiration(self, manager):
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1,  # 1 second TTL
            created_by="test",
        )
        # Should be active immediately
        assert manager.check("weather", {}) is not None
        # Wait for expiry
        time.sleep(1.1)
        assert manager.check("weather", {}) is None

    def test_use_count_exhaustion(self, manager):
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
            max_uses=3,
        )
        # Uses 1, 2, 3 should succeed
        for _ in range(3):
            decision = manager.check("weather", {})
            assert decision is not None
            assert decision.verdict == ActionVerdict.ALLOW
        # Use 4 should return None (exhausted)
        assert manager.check("weather", {}) is None

    def test_deny_wins_over_allow(self, manager):
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.DENY,
            duration_seconds=1800,
            created_by="test",
        )
        decision = manager.check("weather", {})
        assert decision is not None
        assert decision.verdict == ActionVerdict.DENY

    def test_max_duration_cap(self, manager):
        max_seconds = int(manager._config.absolute_max_duration_hours * 3600)
        with pytest.raises(ValueError, match="exceeds maximum"):
            manager.create_override(
                pattern="weather",
                action=ActionVerdict.ALLOW,
                duration_seconds=max_seconds + 1,
                created_by="test",
            )

    def test_excluded_tools_rejected(self, manager):
        with pytest.raises(ValueError, match="excluded"):
            manager.create_override(
                pattern="delete_*",
                action=ActionVerdict.ALLOW,
                duration_seconds=1800,
                created_by="test",
            )

    def test_wildcard_rejected_when_excluded_exist(self, manager):
        with pytest.raises(ValueError, match="Wildcard"):
            manager.create_override(
                pattern="*",
                action=ActionVerdict.ALLOW,
                duration_seconds=1800,
                created_by="test",
            )

    def test_disabled_config(self):
        config = OverrideConfig(enabled=False)
        mgr = TemporaryOverrideManager(config)
        with pytest.raises(ValueError, match="disabled"):
            mgr.create_override(
                pattern="weather",
                action=ActionVerdict.ALLOW,
                duration_seconds=1800,
                created_by="test",
            )

    def test_expired_gc_on_list(self, manager):
        manager.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1,
            created_by="test",
        )
        time.sleep(1.1)
        active = manager.list_active()
        assert len(active) == 0

    def test_audit_logged_on_create(self, manager_with_audit):
        mgr = manager_with_audit
        mgr.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        mgr._audit.log_override_created.assert_called_once()

    def test_audit_logged_on_revoke(self, manager_with_audit):
        mgr = manager_with_audit
        mgr.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        mgr.revoke_override("weather")
        mgr._audit.log_override_revoked.assert_called_once()

    def test_audit_logged_on_hit(self, manager_with_audit):
        mgr = manager_with_audit
        mgr.create_override(
            pattern="weather",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        mgr.check("weather", {})
        mgr._audit.log_override_hit.assert_called_once()

    def test_thread_safety(self, manager):
        """Concurrent create/check should not raise."""
        errors = []

        def worker(i):
            try:
                manager.create_override(
                    pattern=f"tool_{i}",
                    action=ActionVerdict.ALLOW,
                    duration_seconds=1800,
                    created_by="test",
                )
                manager.check(f"tool_{i}", {})
                manager.list_active()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_glob_batch_pattern(self, manager):
        """Pattern like gmail_modify.batch_* matches specific tools."""
        manager.create_override(
            pattern="gmail_modify.batch_*",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        assert manager.check("gmail_modify.batch_label", {}) is not None
        assert manager.check("gmail_modify.batch_trash", {}) is not None
        assert manager.check("gmail_modify.single", {}) is None

    def test_no_excluded_tools_allows_wildcard(self):
        """Wildcard works when excluded_tools is empty."""
        config = OverrideConfig(excluded_tools=[])
        mgr = TemporaryOverrideManager(config)
        ov = mgr.create_override(
            pattern="*",
            action=ActionVerdict.ALLOW,
            duration_seconds=1800,
            created_by="test",
        )
        assert ov is not None
        assert mgr.check("anything", {}) is not None


# --- Integration: /allow, /deny, /allows in ChatServer ---


class TestChatOverrideCommands:
    """Test slash commands by calling ChatServer._handle_allow/deny/allows directly."""

    @pytest.fixture
    def chat_server(self):
        """Build a minimal ChatServer with guardian enabled."""
        from unittest.mock import patch

        from guardian.core import Guardian
        from guardian.types import GuardianConfig

        # Disable all heavyweight components
        config = GuardianConfig(
            fast_classifier={"enabled": False},
            llm_judge={"enabled": False},
            policy={"enabled": False},
            audit={"enabled": False},
            coherence={"enabled": False},
            drift={"enabled": False},
        )
        with patch("creel.chat.ChatServer.__init__", return_value=None):
            from creel.chat import ChatServer

            server = ChatServer.__new__(ChatServer)
        server._guardian = Guardian(config)
        return server

    def test_allow_creates_override(self, chat_server):
        result = chat_server._handle_allow("user1", "/allow weather 15m")
        assert "Allowing" in result
        assert "weather" in result

    def test_allow_default_duration(self, chat_server):
        result = chat_server._handle_allow("user1", "/allow weather")
        assert "Allowing" in result

    def test_allow_with_use_count(self, chat_server):
        result = chat_server._handle_allow("user1", "/allow weather 5x 30m")
        assert "5 uses" in result

    def test_deny_revokes_override(self, chat_server):
        chat_server._handle_allow("user1", "/allow weather 30m")
        result = chat_server._handle_deny("user1", "/deny weather")
        assert "Revoked" in result

    def test_deny_not_found(self, chat_server):
        result = chat_server._handle_deny("user1", "/deny nonexistent")
        assert "No active override" in result

    def test_allows_lists_overrides(self, chat_server):
        chat_server._handle_allow("user1", "/allow weather 30m")
        chat_server._handle_allow("user1", "/allow github.* 1h")
        result = chat_server._handle_allows("user1")
        assert "weather" in result
        assert "github.*" in result

    def test_allows_empty(self, chat_server):
        result = chat_server._handle_allows("user1")
        assert "No active" in result

    def test_allow_invalid_duration(self, chat_server):
        result = chat_server._handle_allow("user1", "/allow weather xyz")
        assert "Invalid duration" in result

    def test_allow_excluded_tool(self, chat_server):
        result = chat_server._handle_allow("user1", "/allow delete_* 30m")
        assert "Cannot create" in result

    def test_allow_no_guardian(self):
        from unittest.mock import patch

        with patch("creel.chat.ChatServer.__init__", return_value=None):
            from creel.chat import ChatServer

            server = ChatServer.__new__(ChatServer)
        server._guardian = None
        result = server._handle_allow("user1", "/allow weather 30m")
        assert "not enabled" in result

    def test_allow_missing_pattern(self, chat_server):
        result = chat_server._handle_allow("user1", "/allow ")
        assert "Usage" in result

    def test_wildcard_requires_confirm(self, chat_server):
        result = chat_server._handle_allow("user1", "/allow * 30m")
        assert "confirm" in result.lower()

    def test_wildcard_with_confirm(self, chat_server):
        # Wildcard is still blocked by excluded_tools (delete_*)
        result = chat_server._handle_allow("user1", "/allow * 30m confirm")
        assert "Cannot create" in result


# --- _build_approval_hint ---


class TestApprovalHint:
    def test_hint_generated(self):
        from creel.agent import _build_approval_hint

        counts = {"github.create_issue": 4}
        hint = _build_approval_hint(counts, 3)
        assert "/allow" in hint
        assert "github.create_issue" in hint

    def test_no_hint_below_threshold(self):
        from creel.agent import _build_approval_hint

        counts = {"github.create_issue": 2}
        hint = _build_approval_hint(counts, 3)
        assert hint == ""

    def test_no_hint_empty(self):
        from creel.agent import _build_approval_hint

        hint = _build_approval_hint({}, 3)
        assert hint == ""
