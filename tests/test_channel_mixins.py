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
from creel.channels.message import AttachmentType
from creel.channels.mixins import (
    BridgeClientMixin,
    FormattingMixin,
    HealthCheckMixin,
    MediaHandlerMixin,
    MessageQueueMixin,
    PollingChannelMixin,
    RetryMixin,
)

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
# RetryMixin
# ---------------------------------------------------------------------------


class _RetryUser(RetryMixin):
    pass


class TestRetryMixin:
    def test_successful_call_returns_value(self):
        user = _RetryUser()
        result = user._retry_with_backoff(lambda: 42)
        assert result == 42

    def test_retries_on_failure_then_succeeds(self):
        user = _RetryUser()
        user._retry_base_delay = 0.01

        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient")
            return "ok"

        result = user._retry_with_backoff(flaky)
        assert result == "ok"
        assert call_count == 3

    def test_raises_after_max_retries(self):
        user = _RetryUser()
        user._max_retries = 2
        user._retry_base_delay = 0.01

        import pytest

        with pytest.raises(ValueError, match="always fails"):
            user._retry_with_backoff(lambda: (_ for _ in ()).throw(ValueError("always fails")))

    def test_passes_args_and_kwargs(self):
        user = _RetryUser()
        result = user._retry_with_backoff(lambda x, y=0: x + y, 3, y=7)
        assert result == 10

    def test_calculate_backoff_formula(self):
        assert RetryMixin._calculate_backoff(5, 0) == 5
        assert RetryMixin._calculate_backoff(5, 1) == 10
        assert RetryMixin._calculate_backoff(5, 2) == 20
        assert RetryMixin._calculate_backoff(5, 3) == 40
        assert RetryMixin._calculate_backoff(5, 4) == 60  # capped at max_backoff=60

    def test_calculate_backoff_custom_max(self):
        assert RetryMixin._calculate_backoff(5, 10, max_backoff=30) == 30


# ---------------------------------------------------------------------------
# MessageQueueMixin
# ---------------------------------------------------------------------------


class _QueuedChannel(MessageQueueMixin, Channel):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def listen(self, callback):
        pass

    def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))


class TestMessageQueueMixin:
    def test_enqueue_and_flush(self):
        ch = _QueuedChannel()
        ch._enqueue("alice", "hello")
        ch._enqueue("bob", "world")
        assert ch.queue_size == 2

        sent = ch._flush_queue()
        assert sent == 2
        assert ch.sent == [("alice", "hello"), ("bob", "world")]
        assert ch.queue_size == 0

    def test_empty_flush_returns_zero(self):
        ch = _QueuedChannel()
        assert ch._flush_queue() == 0
        assert ch.sent == []

    def test_max_queue_size_drops_oldest(self):
        ch = _QueuedChannel()
        ch._max_queue_size = 2
        ch._enqueue("a", "1")
        ch._enqueue("b", "2")
        ch._enqueue("c", "3")  # drops ("a", "1")
        assert ch.queue_size == 2

        ch._flush_queue()
        assert ch.sent == [("b", "2"), ("c", "3")]

    def test_rate_limited_send(self):
        ch = _QueuedChannel()
        ch._rate_limit_delay = 0.05  # 50ms

        start = time.monotonic()
        ch._rate_limited_send("alice", "one")
        ch._rate_limited_send("alice", "two")
        elapsed = time.monotonic() - start

        assert elapsed >= 0.04  # at least one delay
        assert ch.sent == [("alice", "one"), ("alice", "two")]

    def test_no_rate_limit_when_zero(self):
        ch = _QueuedChannel()
        ch._rate_limit_delay = 0.0

        start = time.monotonic()
        ch._rate_limited_send("alice", "one")
        ch._rate_limited_send("alice", "two")
        elapsed = time.monotonic() - start

        assert elapsed < 0.05  # no delay
        assert len(ch.sent) == 2


# ---------------------------------------------------------------------------
# MediaHandlerMixin
# ---------------------------------------------------------------------------


class _MediaChannel(MediaHandlerMixin):
    pass


class TestMediaHandlerMixin:
    def test_classify_image(self):
        ch = _MediaChannel()
        assert ch._classify_mime_type("image/jpeg") == AttachmentType.IMAGE
        assert ch._classify_mime_type("image/png") == AttachmentType.IMAGE

    def test_classify_audio(self):
        ch = _MediaChannel()
        assert ch._classify_mime_type("audio/mpeg") == AttachmentType.AUDIO

    def test_classify_video(self):
        ch = _MediaChannel()
        assert ch._classify_mime_type("video/mp4") == AttachmentType.VIDEO

    def test_classify_voice(self):
        ch = _MediaChannel()
        assert ch._classify_mime_type("audio/x-caf") == AttachmentType.VOICE
        assert ch._classify_mime_type("audio/caf") == AttachmentType.VOICE
        assert ch._classify_mime_type("audio/amr") == AttachmentType.VOICE
        assert ch._classify_mime_type("audio/ogg") == AttachmentType.VOICE

    def test_classify_unknown_defaults_to_file(self):
        ch = _MediaChannel()
        assert ch._classify_mime_type("application/pdf") == AttachmentType.FILE
        assert ch._classify_mime_type(None) == AttachmentType.FILE

    def test_classify_platform_type(self):
        ch = _MediaChannel()
        ch._platform_type_map = {
            "photo": AttachmentType.IMAGE,
            "voice": AttachmentType.VOICE,
        }
        assert ch._classify_platform_type("photo") == AttachmentType.IMAGE
        assert ch._classify_platform_type("voice") == AttachmentType.VOICE
        assert ch._classify_platform_type("unknown") == AttachmentType.FILE

    def test_download_and_classify_success(self):
        ch = _MediaChannel()
        ch._platform_type_map = {"photo": AttachmentType.IMAGE}

        att = ch._download_and_classify(
            lambda fid: b"image-data",
            "file_123",
            platform_type="photo",
            mime_type="image/jpeg",
            file_name="pic.jpg",
            file_size=100,
        )
        assert att is not None
        assert att.type == AttachmentType.IMAGE
        assert att.data == b"image-data"
        assert att.file_name == "pic.jpg"
        assert att.file_size == 100

    def test_download_and_classify_failure_returns_none(self):
        ch = _MediaChannel()

        def fail(fid):
            raise ConnectionError("download failed")

        att = ch._download_and_classify(fail, "file_123", mime_type="image/jpeg")
        assert att is None

    def test_download_and_classify_uses_mime_when_no_platform_type(self):
        ch = _MediaChannel()
        att = ch._download_and_classify(
            lambda fid: b"data",
            "file_1",
            mime_type="video/mp4",
        )
        assert att is not None
        assert att.type == AttachmentType.VIDEO

    def test_custom_voice_mime_types(self):
        ch = _MediaChannel()
        ch._voice_mime_types = frozenset({"audio/custom-voice"})
        assert ch._classify_mime_type("audio/custom-voice") == AttachmentType.VOICE
        # Default voice types should not match with custom override
        assert ch._classify_mime_type("audio/x-caf") == AttachmentType.AUDIO


# ---------------------------------------------------------------------------
# FormattingMixin
# ---------------------------------------------------------------------------


class _FormattedChannel(FormattingMixin):
    pass


class TestFormattingMixin:
    def test_chunk_text_no_limit(self):
        ch = _FormattedChannel()
        assert ch._chunk_text("hello world") == ["hello world"]

    def test_chunk_text_within_limit(self):
        ch = _FormattedChannel()
        ch._max_message_length = 100
        assert ch._chunk_text("short message") == ["short message"]

    def test_chunk_text_splits_at_newline(self):
        ch = _FormattedChannel()
        text = "line one\nline two\nline three"
        chunks = ch._chunk_text(text, limit=15)
        assert chunks == ["line one", "line two", "line three"]

    def test_chunk_text_hard_split(self):
        ch = _FormattedChannel()
        text = "abcdefghij"  # 10 chars, no newlines
        chunks = ch._chunk_text(text, limit=4)
        assert chunks == ["abcd", "efgh", "ij"]

    def test_chunk_text_uses_class_default(self):
        ch = _FormattedChannel()
        ch._max_message_length = 5
        chunks = ch._chunk_text("abcdefgh")
        assert len(chunks) > 1

    def test_chunk_text_limit_zero_means_no_split(self):
        ch = _FormattedChannel()
        long_text = "a" * 10000
        assert ch._chunk_text(long_text, limit=0) == [long_text]

    def test_truncate_within_limit(self):
        ch = _FormattedChannel()
        ch._max_message_length = 100
        assert ch._truncate("short") == "short"

    def test_truncate_exceeds_limit(self):
        ch = _FormattedChannel()
        result = ch._truncate("abcdefghij", limit=7)
        assert result == "abcd..."
        assert len(result) == 7

    def test_truncate_custom_suffix(self):
        ch = _FormattedChannel()
        result = ch._truncate("abcdefghij", limit=6, suffix="~")
        assert result == "abcde~"

    def test_truncate_no_limit_returns_full(self):
        ch = _FormattedChannel()
        assert ch._truncate("anything") == "anything"


# ---------------------------------------------------------------------------
# HealthCheckMixin
# ---------------------------------------------------------------------------


class _HealthChannel(HealthCheckMixin, Channel):
    def listen(self, callback):
        pass

    def send(self, recipient, text):
        pass


class TestHealthCheckMixin:
    def test_basic_health_running(self):
        ch = _HealthChannel()
        health = ch._basic_health()
        assert health["healthy"] is True
        assert health["channel"] == "_HealthChannel"

    def test_basic_health_stopped(self):
        ch = _HealthChannel()
        ch._stop_requested = True
        health = ch._basic_health()
        assert health["healthy"] is False

    def test_bridge_health_with_bridge(self):
        ch = _HealthChannel()
        ch._bridge = _FakeBridge()
        ch._mode = "polling"
        health = ch._bridge_health()
        assert health["healthy"] is True
        assert health["mode"] == "polling"
        assert health["bridge"]["healthy"] is True

    def test_bridge_health_no_bridge(self):
        ch = _HealthChannel()
        health = ch._bridge_health()
        assert health["healthy"] is False  # no bridge means unhealthy

    def test_bridge_health_exception(self):
        class _BrokenBridge:
            def health(self):
                raise RuntimeError("broken")

        ch = _HealthChannel()
        ch._bridge = _BrokenBridge()
        health = ch._bridge_health()
        assert health["healthy"] is False
        assert "error" in health["bridge"]

    def test_should_reconnect_initially_false(self):
        ch = _HealthChannel()
        assert ch._should_reconnect() is False

    def test_should_reconnect_after_threshold(self):
        ch = _HealthChannel()
        ch._last_healthy_time = time.monotonic() - 200  # 200s ago
        assert ch._should_reconnect(unhealthy_threshold=120.0) is True

    def test_should_reconnect_within_threshold(self):
        ch = _HealthChannel()
        ch._last_healthy_time = time.monotonic() - 10  # 10s ago
        assert ch._should_reconnect(unhealthy_threshold=120.0) is False

    def test_consecutive_health_failures_tracked(self):
        ch = _HealthChannel()
        ch._bridge = _UnhealthyBridge()
        ch._bridge_health()
        assert ch._consecutive_health_failures == 1
        ch._bridge_health()
        assert ch._consecutive_health_failures == 2

    def test_health_failures_reset_on_success(self):
        ch = _HealthChannel()
        ch._bridge = _UnhealthyBridge()
        ch._bridge_health()
        assert ch._consecutive_health_failures == 1

        ch._bridge = _FakeBridge()
        ch._bridge_health()
        assert ch._consecutive_health_failures == 0


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


# ---------------------------------------------------------------------------
# Integration: PollingChannelMixin inherits RetryMixin
# ---------------------------------------------------------------------------


class TestPollingInheritsRetry:
    def test_polling_channel_has_calculate_backoff(self):
        """PollingChannelMixin should have _calculate_backoff from RetryMixin."""
        channel = _StubPollingChannel()
        assert hasattr(channel, "_calculate_backoff")
        assert channel._calculate_backoff(5, 2) == 20

    def test_polling_channel_has_retry_with_backoff(self):
        channel = _StubPollingChannel()
        result = channel._retry_with_backoff(lambda: 99)
        assert result == 99
