"""Tests for WhatsApp channel implementation."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.channels.whatsapp import WhatsAppChannel
from taskrunner.channels.whatsapp_bridge import (
    HttpWhatsAppBridge,
    WhatsAppBridge,
    WhatsAppMessage,
)


class MockBridge(WhatsAppBridge):
    """In-memory bridge for testing."""

    def __init__(
        self,
        messages: list[WhatsAppMessage] | None = None,
        initial_timestamp: datetime | None = None,
    ):
        self.messages = messages or []
        self.sent: list[tuple[str, str]] = []
        self.connected = False
        self._initial_ts = initial_timestamp

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def send_message(self, recipient, text):
        self.sent.append((recipient, text))

    def get_messages_since(self, since):
        return [m for m in self.messages if m.timestamp > since]

    def get_latest_timestamp(self):
        if self._initial_ts is not None:
            return self._initial_ts
        return datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def health(self):
        return {"healthy": self.connected}


def _make_msg(sender="user1", text="hello", ts_seconds=1):
    return WhatsAppMessage(
        sender=sender,
        text=text,
        timestamp=datetime(2025, 1, 1, 12, 0, ts_seconds, tzinfo=timezone.utc),
        message_id=f"msg-{ts_seconds}",
    )


class TestPollingMode:
    def test_poll_loop_processes_messages(self):
        msg = _make_msg(sender="+1234", text="hi there", ts_seconds=1)
        bridge = MockBridge(messages=[msg])

        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            mode="polling",
            poll_interval=1,
            allowed_senders=["+1234"],
        )

        responses = []

        def callback(sender, text):
            responses.append((sender, text))
            channel.stop()
            return f"reply to {text}"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert len(responses) == 1
        assert responses[0] == ("+1234", "hi there")
        assert bridge.sent == [("+1234", "reply to hi there")]
        assert not bridge.connected  # disconnect called after loop ends

    def test_poll_loop_filters_by_allowed_senders(self):
        msgs = [
            _make_msg(sender="+allowed", text="yes", ts_seconds=1),
            _make_msg(sender="+blocked", text="no", ts_seconds=2),
        ]
        bridge = MockBridge(messages=msgs)

        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            mode="polling",
            poll_interval=1,
            allowed_senders=["+allowed"],
        )

        processed = []

        def callback(sender, text):
            processed.append(sender)
            channel.stop()
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert "+allowed" in processed
        assert "+blocked" not in processed


class TestWebhookMode:
    def test_get_webhook_routes_in_webhook_mode(self):
        bridge = MockBridge()
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            mode="webhook",
            webhook_path="/webhooks/wa",
        )
        routes = channel.get_webhook_routes()
        assert routes is not None
        assert len(routes) == 2
        paths = {r["path"] for r in routes}
        assert "/webhooks/wa" in paths
        methods = {r["method"] for r in routes}
        assert "GET" in methods
        assert "POST" in methods

    def test_get_webhook_routes_in_polling_mode_returns_none(self):
        bridge = MockBridge()
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            mode="polling",
        )
        assert channel.get_webhook_routes() is None


class TestHealthCheck:
    def test_healthy_when_connected(self):
        bridge = MockBridge()
        bridge.connected = True
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
        )
        health = channel.health_check()
        assert health["healthy"] is True
        assert health["mode"] == "polling"

    def test_unhealthy_when_stopped(self):
        bridge = MockBridge()
        bridge.connected = True
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
        )
        channel.stop()
        health = channel.health_check()
        assert health["healthy"] is False


class TestSend:
    def test_send_delegates_to_bridge(self):
        bridge = MockBridge()
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
        )
        channel.send("+1234", "test message")
        assert bridge.sent == [("+1234", "test message")]


class TestRegisterPlugin:
    def test_register_plugin_returns_meta_and_factory(self):
        from taskrunner.channels.whatsapp import register_plugin
        from taskrunner.channels.plugin import ChannelCapability

        meta, factory = register_plugin()
        assert meta.id == "whatsapp"
        assert ChannelCapability.SEND in meta.capabilities
        assert ChannelCapability.POLLING in meta.capabilities
        assert ChannelCapability.WEBHOOK in meta.capabilities
