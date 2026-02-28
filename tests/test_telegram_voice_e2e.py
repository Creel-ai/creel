"""End-to-end integration test: Telegram voice flow (MEDIA-010).

Simulates the full path:
  1. Telegram webhook/poll receives a voice message
  2. Voice .oga file is downloaded from the Telegram API (mocked)
  3. Voice file is saved to the media store
  4. TranscriptionService transcribes the audio (mocked Whisper API)
  5. Transcribed text is prepended to the user message
  6. LLM receives the text and generates a response (mocked)
  7. Response is sent back to the Telegram chat
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.channels.message import Attachment, AttachmentType, IncomingMessage
from taskrunner.channels.telegram import TelegramChannel
from taskrunner.channels.telegram_bridge import (
    TelegramMedia,
    TelegramMessage,
    _extract_media,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ogg_bytes() -> bytes:
    """Create minimal OGG file header bytes for testing.

    This is not a valid audio file but is sufficient for testing the
    download/save/transcribe pipeline where actual decoding is mocked.
    """
    # OGG magic bytes ("OggS") followed by minimal header
    return b"OggS" + b"\x00" * 100


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
# Tests: Voice media extraction
# ---------------------------------------------------------------------------


class TestVoiceExtractMedia:
    """Test _extract_media for voice-specific Telegram message data."""

    def test_voice_message_extracted(self):
        msg_data = {
            "voice": {
                "file_id": "voice_abc",
                "mime_type": "audio/ogg",
                "file_size": 15000,
                "duration": 3,
            },
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].file_id == "voice_abc"
        assert media[0].file_type == "voice"
        assert media[0].mime_type == "audio/ogg"
        assert media[0].file_size == 15000

    def test_voice_default_mime_type(self):
        """Voice messages without explicit mime_type default to audio/ogg."""
        msg_data = {
            "voice": {
                "file_id": "voice_no_mime",
                "file_size": 5000,
                "duration": 2,
            },
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].mime_type == "audio/ogg"

    def test_voice_with_text_caption(self):
        """Voice message with a caption text."""
        msg_data = {
            "voice": {
                "file_id": "voice_cap",
                "mime_type": "audio/ogg",
                "file_size": 8000,
                "duration": 4,
            },
            "caption": "listen to this",
        }
        media = _extract_media(msg_data)
        assert len(media) == 1
        assert media[0].file_type == "voice"


# ---------------------------------------------------------------------------
# Tests: Voice download via TelegramChannel
# ---------------------------------------------------------------------------


class TestVoiceDownload:
    def test_downloads_voice_as_voice_attachment(self):
        ogg_bytes = _make_ogg_bytes()
        bridge = MockTelegramBridge(file_data={"voice_id": ogg_bytes})
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])

        media = [
            TelegramMedia(file_id="voice_id", file_type="voice", mime_type="audio/ogg")
        ]
        attachments = channel._download_media(media)

        assert len(attachments) == 1
        assert attachments[0].type == AttachmentType.VOICE
        assert attachments[0].data == ogg_bytes
        assert attachments[0].mime_type == "audio/ogg"

    def test_voice_download_failure_skips(self):
        bridge = MockTelegramBridge(file_data={})
        channel = TelegramChannel(bridge=bridge, allowed_senders=["42"])

        media = [
            TelegramMedia(
                file_id="missing_voice", file_type="voice", mime_type="audio/ogg"
            )
        ]
        attachments = channel._download_media(media)

        assert attachments == []


# ---------------------------------------------------------------------------
# Tests: Polling mode with voice message
# ---------------------------------------------------------------------------


class TestPollingWithVoice:
    def test_voice_message_sends_incoming_message_to_callback(self):
        """Polling loop: voice message should call callback with IncomingMessage."""
        ogg_bytes = _make_ogg_bytes()
        voice_msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="",
            update_id=100,
            is_group=False,
            message_id=1,
            media=[
                TelegramMedia(
                    file_id="voice_poll", file_type="voice", mime_type="audio/ogg"
                )
            ],
        )
        bridge = MockTelegramBridge(
            messages=[voice_msg],
            file_data={"voice_poll": ogg_bytes},
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
            return "I heard you!"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()

        assert len(received) == 1
        assert len(received[0]) == 1  # single IncomingMessage arg
        incoming = received[0][0]
        assert isinstance(incoming, IncomingMessage)
        assert incoming.sender_id == "42"
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.VOICE
        assert incoming.attachments[0].data == ogg_bytes
        assert incoming.channel == "telegram"

        assert bridge.sent == [("42", "I heard you!")]

    def test_voice_only_no_text(self):
        """Voice-only message (no caption): text should be None, still processed."""
        ogg_bytes = _make_ogg_bytes()
        voice_msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="",
            update_id=100,
            is_group=False,
            message_id=1,
            media=[
                TelegramMedia(file_id="v1", file_type="voice", mime_type="audio/ogg")
            ],
        )
        bridge = MockTelegramBridge(
            messages=[voice_msg],
            file_data={"v1": ogg_bytes},
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
            return "ok"

        t = threading.Thread(target=channel.listen, args=(callback,))
        t.start()
        t.join(timeout=5)

        assert len(received) == 1
        incoming = received[0][0]
        assert isinstance(incoming, IncomingMessage)
        assert incoming.text is None
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.VOICE


# ---------------------------------------------------------------------------
# Tests: Webhook mode with voice message
# ---------------------------------------------------------------------------


class TestWebhookWithVoice:
    @pytest.mark.asyncio
    async def test_webhook_voice_sends_incoming_message(self):
        """Webhook: voice message should download and send IncomingMessage."""
        ogg_bytes = _make_ogg_bytes()
        bridge = MockTelegramBridge(file_data={"voice_wh": ogg_bytes})

        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="",
            allowed_senders=["42"],
        )

        received = []

        def callback(*args):
            received.append(args)
            return "voice webhook reply"

        channel.set_webhook_callback(callback)

        payload = {
            "update_id": 300,
            "message": {
                "message_id": 10,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "voice": {
                    "file_id": "voice_wh",
                    "mime_type": "audio/ogg",
                    "file_size": 15000,
                    "duration": 3,
                },
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
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.VOICE
        assert incoming.attachments[0].data == ogg_bytes
        assert incoming.channel == "telegram"

        assert bridge.sent == [("42", "voice webhook reply")]

    @pytest.mark.asyncio
    async def test_webhook_voice_with_caption(self):
        """Webhook: voice message with caption preserves text."""
        ogg_bytes = _make_ogg_bytes()
        bridge = MockTelegramBridge(file_data={"voice_cap": ogg_bytes})

        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="",
            allowed_senders=["42"],
        )

        received = []

        def callback(*args):
            received.append(args)
            return "reply"

        channel.set_webhook_callback(callback)

        payload = {
            "update_id": 301,
            "message": {
                "message_id": 11,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": 42, "type": "private"},
                "caption": "check this recording",
                "voice": {
                    "file_id": "voice_cap",
                    "mime_type": "audio/ogg",
                    "file_size": 20000,
                    "duration": 5,
                },
            },
        }

        request = MagicMock()

        async def _body():
            return json.dumps(payload).encode()

        request.body = _body
        request.headers = {}

        await channel._handle_webhook(request)

        incoming = received[0][0]
        assert incoming.text == "check this recording"
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.VOICE


# ---------------------------------------------------------------------------
# Tests: Full end-to-end flow (Telegram voice → ChatServer)
# ---------------------------------------------------------------------------


class TestE2ETelegramVoice:
    """Full integration test: Telegram voice → ChatServer → transcription → LLM → response."""

    def _make_agent_def(self, tmp_path: Path):
        from taskrunner.models import (
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

    def test_full_voice_flow(self, tmp_path: Path):
        """Simulate complete flow: attachment → store → transcribe → LLM → reply."""
        from taskrunner.chat import ChatServer

        ogg_bytes = _make_ogg_bytes()
        agent_def = self._make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        mock_result = MagicMock()
        mock_result.text = "You said hello!"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 100

        with (
            patch.object(
                server._transcription,
                "transcribe",
                return_value="Hello, this is a test",
            ),
            patch(
                "taskrunner.chat.run_agent_loop", return_value=mock_result
            ) as mock_loop,
        ):
            attachment = Attachment(
                type=AttachmentType.VOICE,
                data=ogg_bytes,
                mime_type="audio/ogg",
            )

            response = server.handle_message(
                "42",
                "what did you say?",
                attachments=[attachment],
            )

        # Verify response
        assert response == "You said hello!"

        # Verify LLM received transcribed text prepended to user message
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]

        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) >= 1
        last_user = user_msgs[-1]
        # Voice messages produce plain text (not content blocks)
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: Hello, this is a test" in last_user["content"]
        assert "what did you say?" in last_user["content"]

        # Verify media file was saved to disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.ogg"))
        assert len(saved_files) >= 1
        assert saved_files[0].read_bytes() == ogg_bytes

    def test_voice_only_no_accompanying_text(self, tmp_path: Path):
        """Voice message with no text — transcription becomes the entire message."""
        from taskrunner.chat import ChatServer

        ogg_bytes = _make_ogg_bytes()
        agent_def = self._make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        mock_result = MagicMock()
        mock_result.text = "I understood your voice."
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 80

        with (
            patch.object(
                server._transcription, "transcribe", return_value="Just a voice note"
            ),
            patch(
                "taskrunner.chat.run_agent_loop", return_value=mock_result
            ) as mock_loop,
        ):
            attachment = Attachment(
                type=AttachmentType.VOICE,
                data=ogg_bytes,
                mime_type="audio/ogg",
            )

            response = server.handle_message(
                "42",
                "",
                attachments=[attachment],
            )

        assert response == "I understood your voice."

        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]

        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: Just a voice note" in last_user["content"]

    def test_transcription_failure_shows_fallback(self, tmp_path: Path):
        """When transcription fails, a fallback message is sent to the LLM."""
        from taskrunner.chat import ChatServer

        ogg_bytes = _make_ogg_bytes()
        agent_def = self._make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        mock_result = MagicMock()
        mock_result.text = "I couldn't understand the audio."
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 60

        with (
            patch.object(server._transcription, "transcribe", return_value=""),
            patch(
                "taskrunner.chat.run_agent_loop", return_value=mock_result
            ) as mock_loop,
        ):
            attachment = Attachment(
                type=AttachmentType.VOICE,
                data=ogg_bytes,
                mime_type="audio/ogg",
            )

            response = server.handle_message(
                "42",
                "",
                attachments=[attachment],
            )

        assert response == "I couldn't understand the audio."

        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]

        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert "[Voice message: transcription failed]" in last_user["content"]

        # File should still be saved to disk even if transcription failed
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.ogg"))
        assert len(saved_files) >= 1

    def test_polling_e2e_voice_flow(self, tmp_path: Path):
        """Full E2E: Telegram poll receives voice → download → ChatServer transcribes → reply sent."""
        from taskrunner.chat import ChatServer

        ogg_bytes = _make_ogg_bytes()
        agent_def = self._make_agent_def(tmp_path)

        mock_result = MagicMock()
        mock_result.text = "Got your voice message!"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 150

        voice_msg = TelegramMessage(
            sender_id="42",
            sender_username="alice",
            chat_id="42",
            text="",
            update_id=100,
            is_group=False,
            message_id=1,
            media=[
                TelegramMedia(
                    file_id="tg_voice_1", file_type="voice", mime_type="audio/ogg"
                )
            ],
        )
        bridge = MockTelegramBridge(
            messages=[voice_msg],
            file_data={"tg_voice_1": ogg_bytes},
        )

        channel = TelegramChannel(
            bridge=bridge,
            mode="polling",
            poll_timeout=1,
            allowed_senders=["42"],
        )

        server = ChatServer(agent_def)

        with (
            patch.object(
                server._transcription,
                "transcribe",
                return_value="Hello, this is a test",
            ),
            patch(
                "taskrunner.chat.run_agent_loop", return_value=mock_result
            ) as mock_loop,
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

            def run_poll():
                channel.listen(callback)

            t = threading.Thread(target=run_poll)
            t.start()

            time.sleep(1)
            channel.stop()
            t.join(timeout=5)

        # Verify response was sent back through the bridge
        assert len(bridge.sent) >= 1
        assert bridge.sent[0] == ("42", "Got your voice message!")

        # Verify the LLM received transcribed text
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]

        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: Hello, this is a test" in last_user["content"]

        # Verify media file saved to disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.ogg"))
        assert len(saved_files) >= 1
        assert saved_files[0].read_bytes() == ogg_bytes

    @pytest.mark.asyncio
    async def test_webhook_e2e_voice_flow(self, tmp_path: Path):
        """Full E2E: Telegram webhook receives voice → download → ChatServer transcribes → reply."""
        from taskrunner.chat import ChatServer

        ogg_bytes = _make_ogg_bytes()
        agent_def = self._make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        mock_result = MagicMock()
        mock_result.text = "I heard you say testing!"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approval = None
        mock_result.last_input_tokens = 120

        bridge = MockTelegramBridge(file_data={"voice_webhook_1": ogg_bytes})

        channel = TelegramChannel(
            bridge=bridge,
            mode="webhook",
            webhook_secret="",
            allowed_senders=["42"],
        )

        with (
            patch.object(
                server._transcription,
                "transcribe",
                return_value="testing one two three",
            ),
            patch(
                "taskrunner.chat.run_agent_loop", return_value=mock_result
            ) as mock_loop,
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

            channel.set_webhook_callback(callback)

            payload = {
                "update_id": 400,
                "message": {
                    "message_id": 20,
                    "from": {"id": 42, "username": "alice"},
                    "chat": {"id": 42, "type": "private"},
                    "voice": {
                        "file_id": "voice_webhook_1",
                        "mime_type": "audio/ogg",
                        "file_size": 15000,
                        "duration": 3,
                    },
                },
            }

            request = MagicMock()

            async def _body():
                return json.dumps(payload).encode()

            request.body = _body
            request.headers = {}

            await channel._handle_webhook(request)

        # Verify response was sent back
        assert len(bridge.sent) >= 1
        assert bridge.sent[0] == ("42", "I heard you say testing!")

        # Verify LLM received transcribed text
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]

        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: testing one two three" in last_user["content"]

        # Verify media file saved to disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.ogg"))
        assert len(saved_files) >= 1
        assert saved_files[0].read_bytes() == ogg_bytes
