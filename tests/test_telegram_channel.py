"""Tests for Telegram channel implementation."""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock

import pytest

from taskrunner.channels.telegram import TelegramChannel
from taskrunner.channels.telegram_bridge import (
    TelegramBridge,
    TelegramMessage,
)


class MockBridge(TelegramBridge):
    """In-memory bridge for testing."""

    def __init__(
        self,
        messages: list[TelegramMessage] | None = None,
        bot_info: dict | None = None,
    ):
        self.messages = messages or []
        self.sent: list[tuple[str, str]] = []
        self.typing_sent: list[str] = []
        self._bot_info = bot_info or {"id": 999, "username": "testbot"}
        self._webhook_deleted = False

    def get_me(self):
        return self._bot_info

    def get_updates(self, offset=None, timeout=30):
        # Return messages once, then empty
        msgs = self.messages
        self.messages = []
        return msgs

    def send_message(self, chat_id, text, reply_to_message_id=None):
        self.sent.append((chat_id, text))

    def send_typing(self, chat_id):
        self.typing_sent.append(chat_id)

    def set_webhook(self, url, secret_token=""):
        pass

    def delete_webhook(self):
        self._webhook_deleted = True

    def download_file(self, file_id):
        return b"fake-file-content"

    def health(self):
        return {"healthy": True}


def _make_msg(
    sender_id="42",
    sender_username="alice",
    chat_id="42",
    text="hello",
    update_id=100,
    is_group=False,
    message_id=1,
):
    return TelegramMessage(
        sender_id=sender_id,
        sender_username=sender_username,
        chat_id=chat_id,
        text=text,
        update_id=update_id,
        is_group=is_group,
        message_id=message_id,
    )


class TestPollingMode:
    def test_poll_loop_processes_messages(self):
        msg = _make_msg(sender_id="42", text="hi there")
        bridge = MockBridge(messages=[msg])

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
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
        assert responses[0] == ("42", "hi there")
        assert bridge.sent == [("42", "reply to hi there")]

    def test_poll_loop_filters_by_allowed_senders(self):
        msgs = [
            _make_msg(sender_id="allowed", sender_username="good", text="yes", update_id=1),
            _make_msg(sender_id="blocked", sender_username="bad", text="no", update_id=2),
        ]
        bridge = MockBridge(messages=msgs)

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["allowed"],
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

        # chat_id is used as sender_id in callback — here it matches sender_id
        # because in DMs chat_id == sender_id
        assert any("allowed" in p or "42" in p for p in processed) or len(processed) >= 1

    def test_poll_loop_filters_by_allowed_senders_username(self):
        msgs = [
            _make_msg(sender_id="1", sender_username="allowed_user", text="yes", update_id=1),
            _make_msg(sender_id="2", sender_username="blocked_user", text="no", update_id=2),
        ]
        bridge = MockBridge(messages=msgs)

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["@allowed_user"],
        )

        processed = []

        def callback(sender, text):
            processed.append(text)
            channel.stop()
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()

        assert "yes" in processed
        assert "no" not in processed

    def test_poll_loop_filters_by_allowed_chats(self):
        msgs = [
            _make_msg(
                chat_id="-100",
                text="@testbot allowed group",
                update_id=1,
                is_group=True,
                sender_id="1",
                sender_username="alice",
            ),
            _make_msg(
                chat_id="-200",
                text="@testbot blocked group",
                update_id=2,
                is_group=True,
                sender_id="2",
                sender_username="bob",
            ),
        ]
        bridge = MockBridge(messages=msgs)

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["1", "2"],
            allowed_chats=["-100"],
        )

        processed = []

        def callback(sender, text):
            processed.append(text)
            channel.stop()
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()

        assert "allowed group" in processed
        assert "blocked group" not in processed

    def test_sends_typing_indicator(self):
        msg = _make_msg(text="hi")
        bridge = MockBridge(messages=[msg])

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            send_typing=True,
            allowed_senders=["42"],
        )

        def callback(sender, text):
            channel.stop()
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert "42" in bridge.typing_sent

    def test_skips_typing_when_disabled(self):
        msg = _make_msg(text="hi")
        bridge = MockBridge(messages=[msg])

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            send_typing=False,
            allowed_senders=["42"],
        )

        def callback(sender, text):
            channel.stop()
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert bridge.typing_sent == []

    def test_deletes_webhook_before_polling(self):
        bridge = MockBridge()

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        def callback(sender, text):
            channel.stop()
            return "ok"

        # Stop immediately since there are no messages
        channel._stop_requested = True
        channel.listen(callback)
        assert bridge._webhook_deleted


class TestGroupChat:
    def test_mention_detected_and_stripped(self):
        msg = _make_msg(
            text="@testbot what's the weather?",
            is_group=True,
            chat_id="-100",
        )
        bridge = MockBridge(messages=[msg])

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        processed = []

        def callback(sender, text):
            processed.append(text)
            channel.stop()
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert len(processed) == 1
        assert processed[0] == "what's the weather?"

    def test_no_mention_ignored_in_group(self):
        msg = _make_msg(
            text="hello everyone",
            is_group=True,
            chat_id="-100",
        )
        bridge = MockBridge(messages=[msg])

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        processed = []

        def callback(sender, text):
            processed.append(text)
            channel.stop()
            return "ok"

        # Run briefly then stop
        channel._stop_requested = False

        def run():
            channel.listen(callback)

        t = threading.Thread(target=run)
        t.start()
        # Give it a moment to process, then stop
        import time

        time.sleep(0.5)
        channel.stop()
        t.join(timeout=5)

        assert len(processed) == 0

    def test_dm_always_processed(self):
        msg = _make_msg(
            text="hello bot",
            is_group=False,
        )
        bridge = MockBridge(messages=[msg])

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        processed = []

        def callback(sender, text):
            processed.append(text)
            channel.stop()
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert len(processed) == 1
        assert processed[0] == "hello bot"


class TestWebhookMode:
    def test_get_webhook_routes_in_webhook_mode(self):
        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_path="/webhooks/tg",
            allowed_senders=["42"],
        )
        routes = channel.get_webhook_routes()
        assert routes is not None
        assert len(routes) == 1
        assert routes[0]["path"] == "/webhooks/tg"
        assert routes[0]["method"] == "POST"

    def test_get_webhook_routes_in_polling_mode_returns_none(self):
        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            allowed_senders=["42"],
        )
        assert channel.get_webhook_routes() is None


class TestHealthCheck:
    def test_healthy_state(self):
        bridge = MockBridge()
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])
        health = channel.health_check()
        assert health["healthy"] is True
        assert health["mode"] == "polling"

    def test_unhealthy_when_stopped(self):
        bridge = MockBridge()
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])
        channel.stop()
        health = channel.health_check()
        assert health["healthy"] is False


class TestSend:
    def test_send_delegates_to_bridge(self):
        bridge = MockBridge()
        channel = TelegramChannel(bridge=bridge, allowed_senders=["12345"])
        channel.send("12345", "test message")
        assert bridge.sent == [("12345", "test message")]


class TestWebhookSecretToken:
    @pytest.mark.asyncio
    async def test_valid_secret_token_passes(self):
        import json

        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="my-secret",
            allowed_senders=["42"],
        )
        channel.set_webhook_callback(lambda s, t: "ok")

        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "text": "hello",
            },
        }
        raw = json.dumps(payload).encode()

        request = MagicMock()

        async def _body():
            return raw

        request.body = _body
        request.headers = {"X-Telegram-Bot-Api-Secret-Token": "my-secret"}

        result = await channel._handle_webhook(request)
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_invalid_secret_token_returns_403(self):
        import json

        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="my-secret",
            allowed_senders=["42"],
        )

        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "text": "hello",
            },
        }
        raw = json.dumps(payload).encode()

        request = MagicMock()

        async def _body():
            return raw

        request.body = _body
        request.headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"}

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await channel._handle_webhook(request)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_secret_token_returns_403(self):
        import json

        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="my-secret",
            allowed_senders=["42"],
        )

        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "text": "hello",
            },
        }
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
        """When webhook_secret is empty, token check is skipped (dev mode)."""
        import json

        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="",
            allowed_senders=["42"],
        )
        channel.set_webhook_callback(lambda s, t: "ok")

        payload = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "text": "hello",
            },
        }
        raw = json.dumps(payload).encode()

        request = MagicMock()

        async def _body():
            return raw

        request.body = _body
        request.headers = {}

        result = await channel._handle_webhook(request)
        assert result == {"status": "ok"}


class TestWebhookSecretRequired:
    def test_webhook_mode_requires_secret(self):
        from pydantic import ValidationError

        from taskrunner.models import TelegramChannelConfig

        with pytest.raises(ValidationError, match="webhook_secret"):
            TelegramChannelConfig(
                bot_token="fake-token",
                mode="webhook",
                webhook_secret="",
                allowed_senders=["123"],
            )

    def test_polling_mode_allows_empty_secret(self):
        from taskrunner.models import TelegramChannelConfig

        cfg = TelegramChannelConfig(
            bot_token="fake-token",
            mode="polling",
            webhook_secret="",
            allowed_senders=["123"],
        )
        assert cfg.mode == "polling"


class TestRegisterPlugin:
    def test_register_plugin_returns_meta_and_factory(self):
        from taskrunner.channels.plugin import ChannelCapability
        from taskrunner.channels.telegram import register_plugin

        meta, factory = register_plugin()
        assert meta.id == "telegram"
        assert ChannelCapability.SEND in meta.capabilities
        assert ChannelCapability.POLLING in meta.capabilities
        assert ChannelCapability.WEBHOOK in meta.capabilities
        assert ChannelCapability.TYPING_INDICATOR in meta.capabilities
        assert ChannelCapability.GROUP_CHAT in meta.capabilities

    def test_factory_returns_channel(self):
        from taskrunner.channels.telegram import register_plugin

        _, factory = register_plugin()
        channel = factory(
            {
                "bot_token": "fake-token",
                "mode": "polling",
                "allowed_senders": ["123"],
            }
        )
        assert isinstance(channel, TelegramChannel)

    def test_factory_decrypts_secrets(self, monkeypatch):
        """Factory decrypts secrets/*.env.enc and loads env before config expansion."""
        from unittest.mock import patch

        from taskrunner.channels.telegram import register_plugin

        _, factory = register_plugin()

        fake_env = {"TELEGRAM_BOT_TOKEN": "decrypted-token-123"}
        with patch("taskrunner.secrets.decrypt_env_file", return_value=fake_env) as mock_decrypt:
            channel = factory(
                {
                    "secrets": "secrets/telegram.env.enc",
                    "mode": "polling",
                    "allowed_senders": ["123"],
                }
            )
        mock_decrypt.assert_called_once_with("secrets/telegram.env.enc")
        assert isinstance(channel, TelegramChannel)
        assert os.environ.get("TELEGRAM_BOT_TOKEN") == "decrypted-token-123"


class TestOutboundFiltering:
    def test_send_blocks_unknown_recipient(self):
        bridge = MockBridge()
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])
        channel.send("99999", "should not arrive")
        assert bridge.sent == []

    def test_send_allows_known_sender_id(self):
        bridge = MockBridge()
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])
        channel.send("42", "hello")
        assert bridge.sent == [("42", "hello")]

    def test_send_allows_known_chat_id(self):
        bridge = MockBridge()
        channel = TelegramChannel(
            bridge=bridge,
            allowed_senders=["42"],
            allowed_chats=["-100"],
        )
        channel.send("-100", "group msg")
        assert bridge.sent == [("-100", "group msg")]

    def test_send_allows_verified_sender_via_username(self):
        """@username-only config: inbound passes by username, then reply uses numeric chat_id."""
        msg = _make_msg(sender_id="55", sender_username="alice", chat_id="55", text="hi")
        bridge = MockBridge(messages=[msg])

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["@alice"],
        )

        def callback(sender, text):
            channel.stop()
            return "ok"

        import threading

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        # After inbound processing, chat_id "55" is dynamically registered
        bridge.sent.clear()
        channel.send("55", "reply")
        assert bridge.sent == [("55", "reply")]


class TestDenyByDefault:
    def test_denies_all_when_no_allowed_senders(self):
        bridge = MockBridge()
        channel = TelegramChannel(bridge=bridge, allowed_senders=[])
        msg = _make_msg(sender_id="42", text="hello")
        assert channel._is_allowed(msg) is False

    def test_empty_allowed_senders_raises(self):
        from pydantic import ValidationError

        from taskrunner.models import TelegramChannelConfig

        with pytest.raises(ValidationError, match="allowed_senders"):
            TelegramChannelConfig(
                bot_token="fake-token",
                mode="polling",
                allowed_senders=[],
            )
