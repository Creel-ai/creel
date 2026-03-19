"""Tests for sender allowlisting — SenderStore + SenderGate."""

from __future__ import annotations

from pathlib import Path

import pytest

from creel.channels.sender_gate import SenderGate, SenderPolicy
from creel.channels.sender_store import SenderStore

# ---------------------------------------------------------------------------
# SenderStore unit tests
# ---------------------------------------------------------------------------


class TestSenderStore:
    def test_add_pending_and_get(self, tmp_path: Path):
        store = SenderStore(tmp_path, "test-channel")
        record = store.add_pending("user1", "Alice")
        assert record.sender_id == "user1"
        assert record.status == "pending"
        assert record.display_name == "Alice"
        assert store.get("user1") is not None

    def test_add_pending_idempotent(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        r1 = store.add_pending("u1", "A")
        r2 = store.add_pending("u1", "B")
        assert r1.sender_id == r2.sender_id
        # display_name should not change on second add
        assert r2.display_name == "A"

    def test_approve(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        result = store.approve("u1", resolved_by="owner")
        assert result is not None
        assert result.status == "approved"
        assert result.resolved_by == "owner"
        assert store.is_approved("u1")
        assert not store.is_denied("u1")

    def test_deny(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        result = store.deny("u1", resolved_by="owner")
        assert result is not None
        assert result.status == "denied"
        assert store.is_denied("u1")
        assert not store.is_approved("u1")

    def test_approve_nonexistent_returns_none(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        assert store.approve("nope") is None

    def test_deny_nonexistent_returns_none(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        assert store.deny("nope") is None

    def test_hold_and_release_messages(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        store.hold_message("u1", {"sender_id": "u1", "text": "hi"})
        store.hold_message("u1", {"sender_id": "u1", "text": "hello"})

        held = store.release_held_messages("u1")
        assert len(held) == 2
        assert held[0]["text"] == "hi"
        assert held[1]["text"] == "hello"

        # Second release should be empty
        assert store.release_held_messages("u1") == []

    def test_hold_message_unknown_sender_is_noop(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.hold_message("ghost", {"text": "boo"})
        assert store.release_held_messages("ghost") == []

    def test_get_pending(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        store.add_pending("u2")
        store.approve("u1")
        pending = store.get_pending()
        assert len(pending) == 1
        assert pending[0].sender_id == "u2"

    def test_persistence_roundtrip(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1", "Alice")
        store.approve("u1")
        store.add_pending("u2", "Bob")
        store.hold_message("u2", {"text": "hey"})

        # New instance reads from disk
        store2 = SenderStore(tmp_path, "ch")
        assert store2.is_approved("u1")
        assert store2.get("u2") is not None
        assert store2.get("u2").status == "pending"
        held = store2.release_held_messages("u2")
        assert len(held) == 1

    def test_cleanup_removes_old_records(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("old1")
        # Manually backdate the record
        record = store.get("old1")
        record.created_at = "2020-01-01T00:00:00+00:00"
        store._save()

        removed = store.cleanup(max_age_hours=1)
        assert removed == 1
        assert store.get("old1") is None

    def test_cleanup_keeps_approved(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        store.approve("u1")
        # Backdate
        record = store.get("u1")
        record.created_at = "2020-01-01T00:00:00+00:00"
        store._save()

        removed = store.cleanup(max_age_hours=1)
        assert removed == 0
        assert store.is_approved("u1")

    def test_is_approved_and_is_denied_for_unknown(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        assert not store.is_approved("ghost")
        assert not store.is_denied("ghost")

    def test_channel_id_rejects_path_traversal(self, tmp_path: Path):
        with pytest.raises(ValueError, match="path separators"):
            SenderStore(tmp_path, "../etc")
        with pytest.raises(ValueError, match="path separators"):
            SenderStore(tmp_path, "foo/bar")
        with pytest.raises(ValueError, match="path separators"):
            SenderStore(tmp_path, "foo\\bar")

    def test_hold_message_caps_at_limit(self, tmp_path: Path):
        from creel.channels.sender_store import _MAX_HELD_PER_SENDER

        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        for i in range(_MAX_HELD_PER_SENDER + 10):
            store.hold_message("u1", {"sender_id": "u1", "text": f"msg-{i}"})
        held = store.release_held_messages("u1")
        assert len(held) == _MAX_HELD_PER_SENDER
        # Oldest messages were dropped, newest retained
        assert held[-1]["text"] == f"msg-{_MAX_HELD_PER_SENDER + 9}"

    def test_hold_message_truncates_long_text(self, tmp_path: Path):
        from creel.channels.sender_store import _MAX_HELD_TEXT_LEN

        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        long_text = "x" * (_MAX_HELD_TEXT_LEN + 1000)
        store.hold_message("u1", {"sender_id": "u1", "text": long_text})
        held = store.release_held_messages("u1")
        assert len(held[0]["text"]) == _MAX_HELD_TEXT_LEN


# ---------------------------------------------------------------------------
# SenderGate policy tests
# ---------------------------------------------------------------------------


class TestSenderGateClosed:
    def test_static_sender_passes(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.CLOSED,
            static_senders={"42"},
            store=store,
            owner_sender_ids={"42"},
            notify_fn=lambda r, t: None,
        )
        result = gate.check("42")
        assert result.allowed is True

    def test_unknown_rejected(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.CLOSED,
            static_senders={"42"},
            store=store,
            owner_sender_ids={"42"},
            notify_fn=lambda r, t: None,
        )
        result = gate.check("99")
        assert result.allowed is False


class TestSenderGateOpen:
    def test_anyone_passes(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.OPEN,
            static_senders=set(),
            store=store,
            owner_sender_ids=set(),
            notify_fn=lambda r, t: None,
        )
        result = gate.check("random-person")
        assert result.allowed is True


class TestSenderGateAllowlist:
    def test_static_sender_passes(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        result = gate.check("owner")
        assert result.allowed is True

    def test_unknown_held_and_owner_notified(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        notifications: list[tuple[str, str]] = []

        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: notifications.append((r, t)),
        )

        result = gate.check("stranger", display_name="@stranger", text="hi there")
        assert result.allowed is False
        assert result.pending is True

        # Owner should have been notified
        assert len(notifications) == 1
        assert "stranger" in notifications[0][1]
        assert "/approve" in notifications[0][1]

        # Message was held
        held = store.release_held_messages("stranger")
        assert len(held) == 1
        assert held[0]["text"] == "hi there"

    def test_approved_sender_passes(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        store.approve("u1")

        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        result = gate.check("u1")
        assert result.allowed is True

    def test_denied_sender_blocked(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        store.deny("u1")

        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        result = gate.check("u1")
        assert result.allowed is False
        assert result.pending is False

    def test_pending_sender_holds_additional_messages(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        notifications: list = []
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: notifications.append(1),
        )

        # First message — creates pending record + notifies
        gate.check("u1", text="msg1")
        assert len(notifications) == 1

        # Second message — still pending, holds but does NOT re-notify
        result = gate.check("u1", text="msg2")
        assert result.allowed is False
        assert result.pending is True
        assert len(notifications) == 1  # no second notification

        held = store.release_held_messages("u1")
        assert len(held) == 2

    def test_auto_approve(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
            auto_approve=True,
        )
        result = gate.check("newuser", text="hi")
        assert result.allowed is True
        assert store.is_approved("newuser")


class TestHandleOwnerResponse:
    def test_approve_command(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1", "Alice")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        reply = gate.handle_owner_response("/approve u1", "owner")
        assert reply is not None
        assert "Approved" in reply
        assert store.is_approved("u1")

    def test_deny_command(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        reply = gate.handle_owner_response("/deny u1", "owner")
        assert reply is not None
        assert "Denied" in reply
        assert store.is_denied("u1")

    def test_pending_command(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1", "Alice")
        store.add_pending("u2", "Bob")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        reply = gate.handle_owner_response("/pending", "owner")
        assert reply is not None
        assert "u1" in reply
        assert "u2" in reply

    def test_pending_empty(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        reply = gate.handle_owner_response("/pending", "owner")
        assert reply == "No pending senders."

    def test_non_owner_cannot_approve(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        reply = gate.handle_owner_response("/approve u1", "stranger")
        assert reply is None
        assert not store.is_approved("u1")

    def test_static_sender_non_owner_cannot_approve(self, tmp_path: Path):
        """A static sender who is NOT the owner should not be able to approve."""
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner", "other_static"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        reply = gate.handle_owner_response("/approve u1", "other_static")
        assert reply is None
        assert not store.is_approved("u1")

    def test_unrelated_message_returns_none(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        assert gate.handle_owner_response("hello bot", "owner") is None

    def test_approve_nonexistent(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        reply = gate.handle_owner_response("/approve ghost", "owner")
        assert reply is not None
        assert "No pending" in reply

    def test_approve_missing_target(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        reply = gate.handle_owner_response("/approve", "owner")
        assert reply is not None
        assert "Usage" in reply

    def test_release_held_messages(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        store.hold_message("u1", {"sender_id": "u1", "text": "msg1"})
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        held = gate.release_held_messages("u1")
        assert len(held) == 1
        assert held[0]["text"] == "msg1"

    def test_replay_held_calls_callback_and_send(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        store.add_pending("u1")
        store.hold_message("u1", {"sender_id": "u1", "text": "hello"})
        store.hold_message("u1", {"sender_id": "u1", "text": "world"})
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )

        replies: list[tuple[str, str]] = []
        sent: list[tuple[str, str]] = []

        def callback(sender, text):
            replies.append((sender, text))
            return f"re: {text}"

        def send_fn(recipient, text):
            sent.append((recipient, text))

        gate.replay_held("/approve u1", callback, send_fn)
        assert replies == [("u1", "hello"), ("u1", "world")]
        assert sent == [("u1", "re: hello"), ("u1", "re: world")]

    def test_replay_held_ignores_non_approve(self, tmp_path: Path):
        store = SenderStore(tmp_path, "ch")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: None,
        )
        # Should be a no-op
        gate.replay_held("/deny u1", lambda s, t: "x", lambda r, t: None)


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestConfigModels:
    def test_telegram_defaults_to_closed(self):
        from creel.models import TelegramChannelConfig

        cfg = TelegramChannelConfig(bot_token="tok", allowed_senders=["42"])
        assert cfg.sender_policy == "closed"

    def test_telegram_closed_requires_senders(self):
        from pydantic import ValidationError

        from creel.models import TelegramChannelConfig

        with pytest.raises(ValidationError, match="allowed_senders"):
            TelegramChannelConfig(bot_token="tok", sender_policy="closed", allowed_senders=[])

    def test_telegram_open_allows_empty_senders(self):
        from creel.models import TelegramChannelConfig

        cfg = TelegramChannelConfig(bot_token="tok", sender_policy="open", allowed_senders=[])
        assert cfg.sender_policy == "open"

    def test_telegram_allowlist_requires_at_least_one_sender(self):
        from pydantic import ValidationError

        from creel.models import TelegramChannelConfig

        with pytest.raises(ValidationError, match="at least one entry"):
            TelegramChannelConfig(
                bot_token="tok",
                sender_policy="allowlist",
                allowed_senders=[],
            )

    def test_telegram_allowlist_with_owner(self):
        from creel.models import TelegramChannelConfig

        cfg = TelegramChannelConfig(
            bot_token="tok",
            sender_policy="allowlist",
            allowed_senders=["42"],
            owner="42",
        )
        assert cfg.owner == "42"

    def test_whatsapp_defaults_to_closed(self):
        from creel.models import WhatsAppChannelConfig

        cfg = WhatsAppChannelConfig(phone_number="+1234")
        assert cfg.sender_policy == "closed"

    def test_imessage_defaults_to_closed(self):
        from creel.models import IMessageChannelConfig

        cfg = IMessageChannelConfig(listen_to="+1234")
        assert cfg.sender_policy == "closed"

    def test_bluebubbles_defaults_to_closed(self):
        from creel.models import BlueBubblesChannelConfig

        cfg = BlueBubblesChannelConfig(server_url="http://localhost:1234")
        assert cfg.sender_policy == "closed"


# ---------------------------------------------------------------------------
# Channel integration tests
# ---------------------------------------------------------------------------


class TestTelegramChannelGateIntegration:
    """Test that TelegramChannel integrates with SenderGate correctly."""

    def test_allowlist_mode_holds_unknown_sender(self, tmp_path: Path):
        from creel.channels.telegram import TelegramChannel
        from creel.channels.telegram_bridge import TelegramMessage

        store = SenderStore(tmp_path, "tg")
        notifications: list[tuple[str, str]] = []

        from tests.test_telegram_channel import MockBridge

        bridge = MockBridge()
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: notifications.append((r, t)),
        )
        channel = TelegramChannel(
            bridge=bridge,
            allowed_senders=["owner"],
            sender_gate=gate,
        )

        msg = TelegramMessage(
            sender_id="stranger",
            sender_username="stranger_user",
            chat_id="stranger",
            text="hello",
            update_id=1,
            is_group=False,
            message_id=1,
        )
        assert channel._is_allowed(msg) is False
        assert len(notifications) == 1
        assert store.get("stranger") is not None

    def test_allowlist_mode_allows_static_sender(self, tmp_path: Path):
        from creel.channels.telegram import TelegramChannel
        from creel.channels.telegram_bridge import TelegramMessage

        store = SenderStore(tmp_path, "tg")

        from tests.test_telegram_channel import MockBridge

        bridge = MockBridge()
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"42"},
            store=store,
            owner_sender_ids={"42"},
            notify_fn=lambda r, t: None,
        )
        channel = TelegramChannel(
            bridge=bridge,
            allowed_senders=["42"],
            sender_gate=gate,
        )

        msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="hello",
            update_id=1,
            is_group=False,
            message_id=1,
        )
        assert channel._is_allowed(msg) is True

    def test_closed_mode_backward_compatible(self):
        """Without a gate, channel behaves exactly as before."""
        from creel.channels.telegram import TelegramChannel
        from creel.channels.telegram_bridge import TelegramMessage
        from tests.test_telegram_channel import MockBridge

        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            allowed_senders=["42"],
        )

        msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="hello",
            update_id=1,
            is_group=False,
            message_id=1,
        )
        assert channel._is_allowed(msg) is True

        unknown = TelegramMessage(
            sender_id="99",
            sender_username="nobody",
            chat_id="99",
            text="hello",
            update_id=2,
            is_group=False,
            message_id=2,
        )
        assert channel._is_allowed(unknown) is False

    def test_approval_flow_replays_messages(self, tmp_path: Path):
        """Owner approves → held messages are replayed through the callback."""

        from creel.channels.telegram import TelegramChannel

        store = SenderStore(tmp_path, "tg")
        notifications: list = []

        from tests.test_telegram_channel import MockBridge

        bridge = MockBridge()
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner"},
            store=store,
            owner_sender_ids={"owner"},
            notify_fn=lambda r, t: notifications.append((r, t)),
        )
        channel = TelegramChannel(
            bridge=bridge,
            allowed_senders=["owner"],
            sender_gate=gate,
        )

        # Simulate unknown sender message being held
        gate.check("stranger", display_name="@stranger", text="hello from stranger")

        # Now owner sends /approve stranger
        # We'll test via the dispatch method directly
        from creel.channels.base import IncomingMessage

        owner_msg = IncomingMessage(
            sender_id="owner",
            text="/approve stranger",
            channel="telegram",
            metadata={},
        )

        replies: list[tuple[str, str]] = []

        def callback(sender_id, text):
            replies.append((sender_id, text))
            return f"replying to {text}"

        # Dispatch the owner's message
        response = channel._dispatch_message(owner_msg, callback)
        assert "Approved" in response

        # The held message should have been replayed
        assert len(replies) == 1
        assert replies[0] == ("stranger", "hello from stranger")
        # And the reply was sent
        assert ("stranger", "replying to hello from stranger") in bridge.sent

    @pytest.mark.asyncio
    async def test_webhook_gate_intercepts_owner_command(self, tmp_path: Path):
        """Gate commands via webhook are intercepted and don't reach the callback."""
        import json
        from unittest.mock import MagicMock

        from creel.channels.telegram import TelegramChannel
        from tests.test_telegram_channel import MockBridge

        store = SenderStore(tmp_path, "tg")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"42"},
            store=store,
            owner_sender_ids={"42"},
            notify_fn=lambda r, t: None,
        )

        # Queue a pending sender first
        gate.check("99", display_name="@stranger", text="hi from webhook")

        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="test-secret",
            allowed_senders=["42"],
            sender_gate=gate,
        )
        channel.set_webhook_callback(lambda s, t: "should not be called")

        # Owner sends /approve 99 via webhook
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "username": "owner"},
                "chat": {"id": 42, "type": "private"},
                "text": "/approve 99",
            },
        }
        raw = json.dumps(payload).encode()
        request = MagicMock()

        async def _body():
            return raw

        request.body = _body
        request.headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}

        result = await channel._handle_webhook(request)
        assert result == {"status": "ok"}
        assert store.is_approved("99")
        # The gate reply was sent back to the owner
        assert any("Approved" in text for _, text in bridge.sent)


class TestBlueBubblesChannelGateIntegration:
    def test_check_sender_delegates_to_gate(self, tmp_path: Path):
        from creel.channels.bluebubbles import BlueBubblesChannel

        store = SenderStore(tmp_path, "bb")
        notifications: list = []
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"owner@icloud.com"},
            store=store,
            owner_sender_ids={"owner@icloud.com"},
            notify_fn=lambda r, t: notifications.append((r, t)),
        )
        channel = BlueBubblesChannel(
            server_url="http://localhost:1234",
            password="pw",
            allowed_senders=["owner@icloud.com"],
            sender_gate=gate,
        )

        # Static sender allowed
        assert channel._check_sender("owner@icloud.com") is True

        # Unknown sender held
        assert channel._check_sender("stranger@icloud.com", "hello") is False
        assert len(notifications) == 1
        assert store.get("stranger@icloud.com") is not None

    def test_closed_mode_backward_compatible(self):
        from creel.channels.bluebubbles import BlueBubblesChannel

        channel = BlueBubblesChannel(
            server_url="http://localhost:1234",
            password="pw",
            allowed_senders=["allowed@icloud.com"],
        )
        assert channel._check_sender("allowed@icloud.com") is True
        assert channel._check_sender("blocked@icloud.com") is False


class TestIMessageChannelGateIntegration:
    def test_check_sender_delegates_to_gate(self, tmp_path: Path):
        from creel.channels.imessage import IMessageChannel

        store = SenderStore(tmp_path, "im")
        notifications: list = []
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"+1234"},
            store=store,
            owner_sender_ids={"+1234"},
            notify_fn=lambda r, t: notifications.append((r, t)),
        )
        channel = IMessageChannel(
            allowed_senders=["+1234"],
            sender_gate=gate,
        )

        assert channel._check_sender("+1234") is True
        assert channel._check_sender("+9999", "hello") is False
        assert len(notifications) == 1

    def test_closed_mode_backward_compatible(self):
        from creel.channels.imessage import IMessageChannel

        channel = IMessageChannel(allowed_senders=["+1234"])
        assert channel._check_sender("+1234") is True
        assert channel._check_sender("+9999") is False


class TestWhatsAppChannelGateIntegration:
    def test_allowlist_mode_basic_flow(self, tmp_path: Path):
        store = SenderStore(tmp_path, "wa")
        notifications: list[tuple[str, str]] = []

        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"+owner"},
            store=store,
            owner_sender_ids={"+owner"},
            notify_fn=lambda r, t: notifications.append((r, t)),
        )

        # Static sender allowed
        result = gate.check("+owner")
        assert result.allowed is True

        # Unknown held
        result = gate.check("+stranger", text="hi")
        assert result.allowed is False
        assert result.pending is True
        assert len(notifications) == 1

    def test_check_sender_delegates_to_gate(self, tmp_path: Path):
        from creel.channels.whatsapp import WhatsAppChannel
        from tests.test_whatsapp_channel import MockBridge

        store = SenderStore(tmp_path, "wa")
        gate = SenderGate(
            policy=SenderPolicy.ALLOWLIST,
            static_senders={"+owner"},
            store=store,
            owner_sender_ids={"+owner"},
            notify_fn=lambda r, t: None,
        )
        bridge = MockBridge()
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            allowed_senders=["+owner"],
            sender_gate=gate,
        )

        assert channel._check_sender("+owner") is True
        assert channel._check_sender("+stranger", "hi") is False
