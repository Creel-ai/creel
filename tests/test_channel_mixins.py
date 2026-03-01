"""Tests for channel mixin classes."""

from __future__ import annotations

import threading
import time

from creel.channels.base import (
    Attachment,
    Channel,
    IncomingMessage,
    OutgoingMessage,
)
from creel.channels.mixins import BridgeClientMixin, PollingChannelMixin

# ---------------------------------------------------------------------------
# PollingChannelMixin
# ---------------------------------------------------------------------------


class _StubPollingChannel(PollingChannelMixin, Channel):
    """Minimal channel using the polling mixin for testing."""

    def __init__(self, messages: list[IncomingMessage] | None = None, poll_interval: float = 0.05):
        self._queued = list(messages or [])
        self._poll_interval = poll_interval
        self._max_backoff = 1
        self.sent: list[tuple[str, str]] = []

    def _poll_once(self) -> list[IncomingMessage]:
        msgs = self._queued
        self._queued = []
        return msgs

    def listen(self, callback):
        self._run_poll_loop(callback)

    def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))


class TestPollingChannelMixin:
    def test_processes_messages_and_stops(self):
        msg = IncomingMessage(sender_id="alice", text="hello", channel="test")
        channel = _StubPollingChannel(messages=[msg])

        responses = []

        def callback(sender_id, text):
            responses.append((sender_id, text))
            channel.stop()
            return f"reply to {text}"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()

        assert responses == [("alice", "hello")]
        assert channel.sent == [("alice", "reply to hello")]

    def test_multiple_messages_in_one_poll(self):
        msgs = [
            IncomingMessage(sender_id="a", text="one", channel="test"),
            IncomingMessage(sender_id="b", text="two", channel="test"),
        ]
        channel = _StubPollingChannel(messages=msgs)

        received = []

        def callback(sender_id, text):
            received.append(sender_id)
            if len(received) >= 2:
                channel.stop()
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert received == ["a", "b"]

    def test_empty_poll_does_not_crash(self):
        channel = _StubPollingChannel(messages=[])

        def callback(sender_id, text):
            channel.stop()
            return "ok"

        # Stop after a brief period
        def stop_later():
            time.sleep(0.15)
            channel.stop()

        stopper = threading.Thread(target=stop_later)
        stopper.start()

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)
        stopper.join(timeout=5)
        assert not t.is_alive()

    def test_error_in_poll_once_triggers_backoff(self):
        """The loop should survive errors in _poll_once and eventually recover."""
        call_count = 0

        class _ErrorChannel(_StubPollingChannel):
            def _poll_once(self):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("transient error")
                return []

        channel = _ErrorChannel(poll_interval=0.02)

        def stop_later():
            time.sleep(0.15)
            channel.stop()

        stopper = threading.Thread(target=stop_later)
        stopper.start()

        t = threading.Thread(target=channel.listen, args=(lambda s, t: "ok",))
        t.start()
        t.join(timeout=5)
        stopper.join(timeout=5)

        assert call_count >= 2  # recovered after the error


# ---------------------------------------------------------------------------
# BridgeClientMixin
# ---------------------------------------------------------------------------


class _FakeBridge:
    def health(self):
        return {"healthy": True}


class _UnhealthyBridge:
    def health(self):
        return {"healthy": False, "error": "connection refused"}


class _StubBridgeChannel(BridgeClientMixin, Channel):
    def __init__(self, bridge, mode="polling"):
        self._bridge = bridge
        self._mode = mode

    def listen(self, callback):
        pass

    def send(self, recipient, text):
        pass


class TestBridgeClientMixin:
    def test_healthy_bridge(self):
        channel = _StubBridgeChannel(bridge=_FakeBridge())
        health = channel._bridge_health_check()
        assert health["healthy"] is True
        assert health["mode"] == "polling"
        assert health["bridge"]["healthy"] is True
        assert health["channel"] == "_StubBridgeChannel"

    def test_unhealthy_bridge(self):
        channel = _StubBridgeChannel(bridge=_UnhealthyBridge())
        health = channel._bridge_health_check()
        assert health["healthy"] is False

    def test_stopped_channel_is_unhealthy(self):
        channel = _StubBridgeChannel(bridge=_FakeBridge())
        channel._stop_requested = True
        health = channel._bridge_health_check()
        assert health["healthy"] is False

    def test_webhook_mode_reported(self):
        channel = _StubBridgeChannel(bridge=_FakeBridge(), mode="webhook")
        health = channel._bridge_health_check()
        assert health["mode"] == "webhook"


# ---------------------------------------------------------------------------
# Message dataclasses
# ---------------------------------------------------------------------------


class TestIncomingMessage:
    def test_minimal(self):
        msg = IncomingMessage(sender_id="alice", text="hi", channel="test")
        assert msg.sender_id == "alice"
        assert msg.text == "hi"
        assert msg.channel == "test"
        assert msg.group_id is None
        assert msg.media is None
        assert msg.metadata == {}

    def test_with_media_and_metadata(self):
        att = Attachment(data=b"img", filename="photo.jpg", mime_type="image/jpeg")
        msg = IncomingMessage(
            sender_id="bob",
            text="check this",
            channel="telegram",
            group_id="-100",
            media=[att],
            metadata={"message_id": 42},
        )
        assert msg.group_id == "-100"
        assert len(msg.media) == 1
        assert msg.media[0].filename == "photo.jpg"
        assert msg.metadata["message_id"] == 42


class TestOutgoingMessage:
    def test_basic(self):
        msg = OutgoingMessage(recipient="alice", text="hello")
        assert msg.recipient == "alice"
        assert msg.text == "hello"
        assert msg.media is None

    def test_send_message_delegates(self):
        """Channel.send_message() delegates to send() by default."""

        class _MinimalChannel(Channel):
            def __init__(self):
                self.sent: list[tuple[str, str]] = []

            def listen(self, callback):
                pass

            def send(self, recipient, text):
                self.sent.append((recipient, text))

        ch = _MinimalChannel()
        ch.send_message(OutgoingMessage(recipient="alice", text="hello"))
        assert ch.sent == [("alice", "hello")]


class TestAttachment:
    def test_fields(self):
        att = Attachment(data=b"\x89PNG", filename="img.png", mime_type="image/png", size=4)
        assert att.data == b"\x89PNG"
        assert att.filename == "img.png"
        assert att.mime_type == "image/png"
        assert att.size == 4
