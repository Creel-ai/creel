"""End-to-end integration test: Telegram image flow (MEDIA-009).

Simulates the full path:
  1. Telegram webhook receives a photo message
  2. Photo is downloaded from the Telegram API (mocked)
  3. Photo is saved to the media store
  4. VisionProcessor converts image to content blocks
  5. LLM receives image and generates a response (mocked)
  6. Response is sent back to the Telegram chat
  7. Media file exists on disk
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from creel.channels.message import Attachment, AttachmentType, IncomingMessage
from creel.channels.telegram import (
    TelegramChannel,
    _telegram_file_type_to_attachment,
)
from creel.channels.telegram_bridge import (
    TelegramMedia,
    TelegramMessage,
    _extract_media,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png_bytes() -> bytes:
    """Create minimal 1x1 PNG image bytes."""
    import io

    from PIL import Image

    img = Image.new("RGB", (1, 1), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes() -> bytes:
    """Create minimal 1x1 JPEG image bytes."""
    import io

    from PIL import Image

    img = Image.new("RGB", (1, 1), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class MockTelegramBridge:
    """In-memory bridge that returns controlled media downloads."""

    def __init__(
        self,
        messages: list[TelegramMessage] | None = None,
        file_data: dict[str, bytes] | None = None,
    ):
        self.messages = messages or []
        self.file_data = file_data or {}
        self.sent: list[tuple[str, str]] = []
        self.typing_sent: list[str] = []
        self._bot_info = {"id": 999, "username": "testbot"}
        self._webhook_deleted = False

    def get_me(self):
        return self._bot_info

    def get_updates(self, offset=None, timeout=30):
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

    def download_file(self, file_id: str) -> bytes:
        if file_id not in self.file_data:
            raise RuntimeError(f"File {file_id} not found")
        return self.file_data[file_id]

    def health(self):
        return {"healthy": True}


# ---------------------------------------------------------------------------
# Tests: _extract_media helper
# ---------------------------------------------------------------------------


class TestExtractMedia:
    """Test the _extract_media helper that parses Telegram message dicts."""

    def test_photo_message_picks_largest(self):
        msg_data = {
            "photo": [
                {"file_id": "small", "file_size": 100, "width": 90, "height": 90},
                {"file_id": "medium", "file_size": 5000, "width": 320, "height": 320},
                {"file_id": "large", "file_size": 50000, "width": 800, "height": 800},
            ],
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].file_id == "large"
        assert media[0].file_type == "photo"
        assert media[0].mime_type == "image/jpeg"

    def test_voice_message(self):
        msg_data = {
            "voice": {
                "file_id": "voice123",
                "mime_type": "audio/ogg",
                "file_size": 12345,
                "duration": 5,
            },
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].file_type == "voice"
        assert media[0].mime_type == "audio/ogg"

    def test_document_with_image_mime(self):
        msg_data = {
            "document": {
                "file_id": "doc123",
                "file_name": "screenshot.png",
                "mime_type": "image/png",
                "file_size": 99999,
            },
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].file_type == "photo"  # classified as photo based on MIME
        assert media[0].file_name == "screenshot.png"

    def test_document_non_image(self):
        msg_data = {
            "document": {
                "file_id": "doc456",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "file_size": 50000,
            },
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].file_type == "document"

    def test_video_message(self):
        msg_data = {
            "video": {
                "file_id": "vid789",
                "mime_type": "video/mp4",
                "file_size": 500000,
            },
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].file_type == "video"

    def test_audio_message(self):
        msg_data = {
            "audio": {
                "file_id": "aud456",
                "file_name": "song.mp3",
                "mime_type": "audio/mpeg",
                "file_size": 3000000,
            },
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].file_type == "audio"
        assert media[0].file_name == "song.mp3"

    def test_text_only_no_media(self):
        msg_data = {"text": "hello"}
        media = _extract_media(msg_data)
        assert media == []

    def test_parse_message_includes_media(self):
        """_parse_message should populate the media field for photo updates."""
        from creel.channels.telegram_bridge import HttpTelegramBridge

        bridge = HttpTelegramBridge.__new__(HttpTelegramBridge)
        update = {
            "update_id": 100,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "caption": "check this",
                "photo": [
                    {
                        "file_id": "photo_small",
                        "file_size": 100,
                        "width": 90,
                        "height": 90,
                    },
                    {
                        "file_id": "photo_large",
                        "file_size": 50000,
                        "width": 800,
                        "height": 800,
                    },
                ],
            },
        }
        msg = bridge._parse_message(update)
        assert msg is not None
        assert msg.text == "check this"
        assert msg.media is not None
        assert len(msg.media) == 1
        assert msg.media[0].file_id == "photo_large"


# ---------------------------------------------------------------------------
# Tests: Attachment type mapping
# ---------------------------------------------------------------------------


class TestAttachmentTypeMapping:
    def test_photo_maps_to_image(self):
        assert _telegram_file_type_to_attachment("photo") == AttachmentType.IMAGE

    def test_voice_maps_to_voice(self):
        assert _telegram_file_type_to_attachment("voice") == AttachmentType.VOICE

    def test_audio_maps_to_audio(self):
        assert _telegram_file_type_to_attachment("audio") == AttachmentType.AUDIO

    def test_video_maps_to_video(self):
        assert _telegram_file_type_to_attachment("video") == AttachmentType.VIDEO

    def test_document_maps_to_file(self):
        assert _telegram_file_type_to_attachment("document") == AttachmentType.FILE

    def test_unknown_maps_to_file(self):
        assert _telegram_file_type_to_attachment("sticker") == AttachmentType.FILE


# ---------------------------------------------------------------------------
# Tests: TelegramChannel._download_media
# ---------------------------------------------------------------------------


class TestDownloadMedia:
    def test_downloads_and_converts_to_attachments(self):
        jpeg_bytes = _make_jpeg_bytes()
        bridge = MockTelegramBridge(file_data={"photo123": jpeg_bytes})
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])

        media = [TelegramMedia(file_id="photo123", file_type="photo", mime_type="image/jpeg")]
        attachments = channel._download_media(media)

        assert len(attachments) == 1
        assert attachments[0].type == AttachmentType.IMAGE
        assert attachments[0].data == jpeg_bytes
        assert attachments[0].mime_type == "image/jpeg"

    def test_download_failure_skips_attachment(self):
        bridge = MockTelegramBridge(file_data={})  # no files
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])

        media = [TelegramMedia(file_id="missing", file_type="photo")]
        attachments = channel._download_media(media)

        assert attachments == []

    def test_multiple_media_items(self):
        jpeg_bytes = _make_jpeg_bytes()
        bridge = MockTelegramBridge(file_data={"p1": jpeg_bytes, "p2": jpeg_bytes})
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])

        media = [
            TelegramMedia(file_id="p1", file_type="photo", mime_type="image/jpeg"),
            TelegramMedia(file_id="p2", file_type="photo", mime_type="image/jpeg"),
        ]
        attachments = channel._download_media(media)
        assert len(attachments) == 2


# ---------------------------------------------------------------------------
# Tests: Polling mode with photo message
# ---------------------------------------------------------------------------


class TestPollingWithPhoto:
    def test_photo_message_sends_incoming_message_to_callback(self):
        """Polling loop: photo message should call callback with IncomingMessage."""
        jpeg_bytes = _make_jpeg_bytes()
        photo_msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="what is this?",
            update_id=100,
            is_group=False,
            message_id=1,
            media=[TelegramMedia(file_id="photo_abc", file_type="photo", mime_type="image/jpeg")],
        )
        bridge = MockTelegramBridge(
            messages=[photo_msg],
            file_data={"photo_abc": jpeg_bytes},
        )

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        received = []

        def callback(*args):
            received.append(args)
            channel.stop()
            return "I see an image!"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()

        # Should have received exactly one callback with IncomingMessage
        assert len(received) == 1
        assert len(received[0]) == 1  # single IncomingMessage arg
        incoming = received[0][0]
        assert isinstance(incoming, IncomingMessage)
        assert incoming.sender_id == "42"
        assert incoming.text == "what is this?"
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.IMAGE
        assert incoming.attachments[0].data == jpeg_bytes
        assert incoming.channel == "telegram"

        # Response should have been sent back
        assert bridge.sent == [("42", "I see an image!")]

    def test_photo_only_no_text(self):
        """Photo with no caption: text should be None, message still processed."""
        jpeg_bytes = _make_jpeg_bytes()
        photo_msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="",  # no caption
            update_id=100,
            is_group=False,
            message_id=1,
            media=[TelegramMedia(file_id="p1", file_type="photo", mime_type="image/jpeg")],
        )
        bridge = MockTelegramBridge(
            messages=[photo_msg],
            file_data={"p1": jpeg_bytes},
        )

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        received = []

        def callback(*args):
            received.append(args)
            channel.stop()
            return "got it"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert len(received) == 1
        incoming = received[0][0]
        assert isinstance(incoming, IncomingMessage)
        # _extract_text returns None for empty text, so incoming.text should be None
        assert incoming.text is None
        assert len(incoming.attachments) == 1

    def test_text_only_still_works(self):
        """Text-only messages should still use the old (sender_id, text) callback."""
        text_msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="hello",
            update_id=100,
            is_group=False,
            message_id=1,
            media=None,
        )
        bridge = MockTelegramBridge(messages=[text_msg])

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        received = []

        def callback(*args):
            received.append(args)
            channel.stop()
            return "reply"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert len(received) == 1
        assert received[0] == ("42", "hello")  # old-style (sender_id, text)


# ---------------------------------------------------------------------------
# Tests: Webhook mode with photo message
# ---------------------------------------------------------------------------


class TestWebhookWithPhoto:
    @pytest.mark.asyncio
    async def test_webhook_photo_sends_incoming_message(self):
        """Webhook: photo message should download and send IncomingMessage."""
        jpeg_bytes = _make_jpeg_bytes()
        bridge = MockTelegramBridge(file_data={"photo_webhook": jpeg_bytes})

        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="",
            allowed_senders=["42"],
        )

        received = []

        def callback(*args):
            received.append(args)
            return "webhook reply"

        channel.set_webhook_callback(callback)

        payload = {
            "update_id": 200,
            "message": {
                "message_id": 5,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "caption": "look at this",
                "photo": [
                    {
                        "file_id": "photo_webhook",
                        "file_size": 50000,
                        "width": 800,
                        "height": 800,
                    },
                ],
            },
        }

        request = MagicMock()

        async def _body():
            return json.dumps(payload).encode()

        request.body = _body
        request.headers = {}

        result = await channel._handle_webhook(request)
        assert result == {"status": "ok"}

        assert len(received) == 1
        incoming = received[0][0]
        assert isinstance(incoming, IncomingMessage)
        assert incoming.text == "look at this"
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.IMAGE
        assert incoming.attachments[0].data == jpeg_bytes
        assert incoming.channel == "telegram"

        # Verify response was sent
        assert bridge.sent == [("42", "webhook reply")]

    @pytest.mark.asyncio
    async def test_webhook_text_only_still_works(self):
        """Webhook: text-only messages use old-style callback."""
        bridge = MockTelegramBridge()

        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="",
            allowed_senders=["42"],
        )

        received = []

        def callback(*args):
            received.append(args)
            return "text reply"

        channel.set_webhook_callback(callback)

        payload = {
            "update_id": 201,
            "message": {
                "message_id": 6,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "text": "hello webhook",
            },
        }

        request = MagicMock()

        async def _body():
            return json.dumps(payload).encode()

        request.body = _body
        request.headers = {}

        result = await channel._handle_webhook(request)
        assert result == {"status": "ok"}

        assert len(received) == 1
        assert received[0] == ("42", "hello webhook")


# ---------------------------------------------------------------------------
# Tests: Full end-to-end flow (Telegram -> ChatServer)
# ---------------------------------------------------------------------------


class TestE2ETelegramImage:
    """Full integration test: Telegram photo → ChatServer → LLM → response."""

    def _make_agent_def(self, tmp_path: Path):
        from creel.models import (
            AgentConfig,
            AgentDefinition,
            ChannelsConfig,
            LLMConfig,
            MediaConfig,
            SessionConfig,
            WorkspaceConfig,
        )

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        media_dir = tmp_path / "media"
        media_dir.mkdir(exist_ok=True)

        return AgentDefinition(
            system_prompt="You are a test assistant.",
            llm=LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100),
            agent=AgentConfig(max_turns=3),
            session=SessionConfig(
                sessions_dir=str(sessions_dir),
                max_history=50,
                summarize_on_trim=False,
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "nonexistent-workspace")),
            channels=ChannelsConfig(),
            media=MediaConfig(enabled=True, storage_dir=str(media_dir)),
        )

    def test_full_image_flow(self, tmp_path: Path):
        """Simulate complete flow: poll → download → store → vision → LLM → reply."""
        from creel.chat import ChatServer

        jpeg_bytes = _make_jpeg_bytes()
        agent_def = self._make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        # Mock the agent loop to return a response about the image
        mock_result = MagicMock()
        mock_result.text = "I see a blue pixel!"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 100

        mock_vision_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc123"},
        }

        with (
            patch.object(server._vision, "prepare_image", return_value=mock_vision_block),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            # Create an Attachment as if downloaded from Telegram
            attachment = Attachment(
                type=AttachmentType.IMAGE,
                data=jpeg_bytes,
                mime_type="image/jpeg",
            )

            response = server.handle_message(
                "42",
                "What is this image?",
                attachments=[attachment],
            )

        # Verify response
        assert response == "I see a blue pixel!"

        # Verify LLM received content blocks (not a plain string)
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]

        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) >= 1
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], list)
        types = [b["type"] for b in last_user["content"]]
        assert "text" in types
        assert "image" in types

        # Verify media file was saved to disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.jpg"))
        assert len(saved_files) >= 1
        # The saved file should contain the JPEG bytes
        assert saved_files[0].read_bytes() == jpeg_bytes

    def test_full_image_flow_via_incoming_message(self, tmp_path: Path):
        """Simulate the DaemonService path: IncomingMessage → ChatServer."""
        from creel.chat import ChatServer

        jpeg_bytes = _make_jpeg_bytes()
        agent_def = self._make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        mock_result = MagicMock()
        mock_result.text = "Nice photo!"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 50

        mock_vision_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "xyz"},
        }

        incoming = IncomingMessage(
            sender_id="42",
            text="describe this",
            attachments=[
                Attachment(
                    type=AttachmentType.IMAGE,
                    data=jpeg_bytes,
                    mime_type="image/jpeg",
                )
            ],
            channel="telegram",
        )

        with (
            patch.object(server._vision, "prepare_image", return_value=mock_vision_block),
            patch("creel.chat.run_agent_loop", return_value=mock_result),
        ):
            response = server.handle_message(
                incoming.sender_id,
                incoming.text or "",
                attachments=incoming.attachments,
            )

        assert response == "Nice photo!"

        # Verify file on disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.jpg"))
        assert len(saved_files) >= 1

    def test_media_disabled_ignores_attachments(self, tmp_path: Path):
        """When media is disabled, attachments are silently ignored."""
        from creel.models import (
            AgentConfig,
            AgentDefinition,
            ChannelsConfig,
            LLMConfig,
            SessionConfig,
            WorkspaceConfig,
        )

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)

        agent_def = AgentDefinition(
            system_prompt="You are a test assistant.",
            llm=LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100),
            agent=AgentConfig(max_turns=3),
            session=SessionConfig(
                sessions_dir=str(sessions_dir),
                max_history=50,
                summarize_on_trim=False,
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "nonexistent-workspace")),
            channels=ChannelsConfig(),
            media=None,  # media disabled
        )

        from creel.chat import ChatServer

        server = ChatServer(agent_def)

        mock_result = MagicMock()
        mock_result.text = "Just text response"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 50

        attachment = Attachment(
            type=AttachmentType.IMAGE,
            data=_make_jpeg_bytes(),
            mime_type="image/jpeg",
        )

        with patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop:
            response = server.handle_message(
                "42",
                "What is this?",
                attachments=[attachment],
            )

        assert response == "Just text response"
        # Content should be plain text (no image blocks)
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)

    def test_polling_e2e_image_flow(self, tmp_path: Path):
        """Full E2E: Telegram poll receives photo → download → ChatServer processes → reply sent."""
        from creel.chat import ChatServer

        jpeg_bytes = _make_jpeg_bytes()
        agent_def = self._make_agent_def(tmp_path)

        mock_result = MagicMock()
        mock_result.text = "That's a beautiful image!"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 200

        mock_vision_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "encoded"},
        }

        # Set up the Telegram poll message with a photo
        photo_msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="What do you see?",
            update_id=100,
            is_group=False,
            message_id=1,
            media=[TelegramMedia(file_id="tg_photo_1", file_type="photo", mime_type="image/jpeg")],
        )
        bridge = MockTelegramBridge(
            messages=[photo_msg],
            file_data={"tg_photo_1": jpeg_bytes},
        )

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        # The DaemonService wires channel.listen(callback) where callback=send_message
        # which calls ChatServer.handle_message. We simulate this:
        server = ChatServer(agent_def)

        with (
            patch.object(server._vision, "prepare_image", return_value=mock_vision_block),
            patch("creel.chat.run_agent_loop", return_value=mock_result),
        ):

            def callback(*args):
                if len(args) == 1 and isinstance(args[0], IncomingMessage):
                    msg = args[0]
                    return server.handle_message(
                        msg.sender_id,
                        msg.text or "",
                        attachments=msg.attachments,
                    )
                return server.handle_message(args[0], args[1])

            # Run channel with callback that feeds into ChatServer
            def run_poll():
                channel.listen(callback)

            t = threading.Thread(target=run_poll)
            t.start()

            # Wait briefly then stop
            import time

            time.sleep(1)
            channel.stop()
            t.join(timeout=5)

        # Verify response was sent back through the bridge
        assert len(bridge.sent) >= 1
        assert bridge.sent[0] == ("42", "That's a beautiful image!")

        # Verify media file saved to disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.jpg"))
        assert len(saved_files) >= 1
        assert saved_files[0].read_bytes() == jpeg_bytes
