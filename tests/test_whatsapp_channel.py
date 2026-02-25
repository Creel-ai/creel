"""Tests for WhatsApp channel implementation."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from creel.channels.whatsapp import WhatsAppChannel
from creel.channels.whatsapp_bridge import (
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
        assert not t.is_alive()

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
        assert not t.is_alive()

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


class TestWebhookHMAC:
    @pytest.mark.asyncio
    async def test_valid_hmac_signature_passes(self):
        import hashlib
        import hmac as hmac_mod
        import json

        bridge = MockBridge()
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            mode="webhook",
            webhook_verify_token="tok",
            webhook_secret="test-secret",
        )
        channel.set_webhook_callback(lambda s, t: "ok")

        payload = {"entry": []}
        raw = json.dumps(payload).encode()
        sig = hmac_mod.new(b"test-secret", raw, hashlib.sha256).hexdigest()

        request = MagicMock()
        request.body = MagicMock(return_value=raw)
        request.body.return_value = raw
        # Make request.body() an awaitable
        async def _body():
            return raw
        request.body = _body
        request.json = MagicMock(return_value=payload)
        request.headers = {"X-Hub-Signature-256": f"sha256={sig}"}

        result = await channel._handle_webhook(request)
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_invalid_hmac_signature_returns_403(self):
        import json

        bridge = MockBridge()
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            mode="webhook",
            webhook_verify_token="tok",
            webhook_secret="test-secret",
        )

        payload = {"entry": []}
        raw = json.dumps(payload).encode()

        request = MagicMock()
        async def _body():
            return raw
        request.body = _body
        request.headers = {"X-Hub-Signature-256": "sha256=badsignature"}

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await channel._handle_webhook(request)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_signature_returns_403(self):
        import json

        bridge = MockBridge()
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            mode="webhook",
            webhook_verify_token="tok",
            webhook_secret="test-secret",
        )

        payload = {"entry": []}
        raw = json.dumps(payload).encode()

        request = MagicMock()
        async def _body():
            return raw
        request.body = _body
        request.headers = {}

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await channel._handle_webhook(request)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_secret_skips_verification(self):
        """When webhook_secret is empty, HMAC check is skipped (dev mode)."""
        import json

        bridge = MockBridge()
        channel = WhatsAppChannel(
            bridge=bridge,
            phone_number="+9999",
            mode="webhook",
            webhook_verify_token="tok",
            webhook_secret="",
        )
        channel.set_webhook_callback(lambda s, t: "ok")

        payload = {"entry": []}
        raw = json.dumps(payload).encode()

        request = MagicMock()
        async def _body():
            return raw
        request.body = _body
        request.headers = {}

        result = await channel._handle_webhook(request)
        assert result == {"status": "ok"}


class TestWebhookVerifyTokenRequired:
    def test_webhook_mode_requires_verify_token(self):
        from creel.models import WhatsAppChannelConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="webhook_verify_token"):
            WhatsAppChannelConfig(
                phone_number="+1234",
                mode="webhook",
                webhook_verify_token="",
            )

    def test_polling_mode_allows_empty_verify_token(self):
        from creel.models import WhatsAppChannelConfig

        cfg = WhatsAppChannelConfig(
            phone_number="+1234",
            mode="polling",
            webhook_verify_token="",
        )
        assert cfg.mode == "polling"


class TestRegisterPlugin:
    def test_register_plugin_returns_meta_and_factory(self):
        from creel.channels.whatsapp import register_plugin
        from creel.channels.plugin import ChannelCapability

        meta, factory = register_plugin()
        assert meta.id == "whatsapp"
        assert ChannelCapability.SEND in meta.capabilities
        assert ChannelCapability.POLLING in meta.capabilities
        assert ChannelCapability.WEBHOOK in meta.capabilities
