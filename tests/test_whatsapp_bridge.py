"""Tests for WhatsApp bridge implementations."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from creel.channels.whatsapp_bridge import (
    HttpWhatsAppBridge,
    NeonizeWhatsAppBridge,
    WhatsAppMessage,
)


class TestHttpWhatsAppBridge:
    def test_connect_checks_health(self):
        bridge = HttpWhatsAppBridge("http://localhost:8080")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.get", return_value=mock_resp) as mock_get:
            bridge.connect()

        mock_get.assert_called_once_with("http://localhost:8080/health", timeout=5)

    def test_send_message(self):
        bridge = HttpWhatsAppBridge("http://localhost:8080")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            bridge.send_message("+1234", "hello")

        mock_post.assert_called_once_with(
            "http://localhost:8080/send",
            json={"recipient": "+1234", "text": "hello"},
            timeout=30,
        )

    def test_get_messages_since(self):
        bridge = HttpWhatsAppBridge("http://localhost:8080")
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "messages": [
                {
                    "sender": "+1234",
                    "text": "hi",
                    "timestamp": "2025-01-01T12:00:00+00:00",
                    "message_id": "msg-1",
                }
            ]
        }

        with patch("httpx.get", return_value=mock_resp):
            messages = bridge.get_messages_since(ts)

        assert len(messages) == 1
        assert messages[0].sender == "+1234"
        assert messages[0].text == "hi"
        assert isinstance(messages[0], WhatsAppMessage)

    def test_health_success(self):
        bridge = HttpWhatsAppBridge("http://localhost:8080")

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.get", return_value=mock_resp):
            h = bridge.health()

        assert h["healthy"] is True

    def test_health_failure(self):
        bridge = HttpWhatsAppBridge("http://localhost:8080")

        with patch("httpx.get", side_effect=Exception("conn refused")):
            h = bridge.health()

        assert h["healthy"] is False
        assert "error" in h

    def test_disconnect(self):
        bridge = HttpWhatsAppBridge("http://localhost:8080")
        bridge._connected = True
        bridge.disconnect()
        assert bridge._connected is False

    def test_url_trailing_slash_stripped(self):
        bridge = HttpWhatsAppBridge("http://localhost:8080/")
        assert bridge._url == "http://localhost:8080"


class TestNeonizeWhatsAppBridge:
    def test_connect_without_neonize_raises(self):
        bridge = NeonizeWhatsAppBridge("/tmp/auth")

        with patch.dict("sys.modules", {"neonize": None}):
            with pytest.raises(ImportError, match="neonize is required"):
                bridge.connect()

    def test_send_not_implemented(self):
        bridge = NeonizeWhatsAppBridge("/tmp/auth")

        with pytest.raises(NotImplementedError):
            bridge.send_message("+1234", "hi")

    def test_get_messages_not_implemented(self):
        bridge = NeonizeWhatsAppBridge("/tmp/auth")
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)

        with pytest.raises(NotImplementedError):
            bridge.get_messages_since(ts)

    def test_get_latest_timestamp_returns_now(self):
        bridge = NeonizeWhatsAppBridge("/tmp/auth")
        ts = bridge.get_latest_timestamp()
        assert isinstance(ts, datetime)

    def test_disconnect_does_not_raise(self):
        bridge = NeonizeWhatsAppBridge("/tmp/auth")
        bridge.disconnect()  # should not raise
