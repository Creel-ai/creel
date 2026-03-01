"""Tests for ChatServer media attachment processing (MEDIA-007)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from creel.channels.message import Attachment, AttachmentType
from creel.chat import ChatServer
from creel.models import (
    AgentConfig,
    AgentDefinition,
    ChannelsConfig,
    LLMConfig,
    MediaConfig,
    SessionConfig,
    WorkspaceConfig,
)


def _make_agent_def(tmp_path: Path, **overrides) -> AgentDefinition:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    defaults = dict(
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
        media=MediaConfig(enabled=True),
    )
    defaults.update(overrides)
    return AgentDefinition(**defaults)


def _make_agent_result(text: str = "response", **kwargs):
    result = MagicMock()
    result.text = text
    result.turns_used = kwargs.get("turns_used", 1)
    result.tool_calls_made = kwargs.get("tool_calls_made", 0)
    result.stop_reason = kwargs.get("stop_reason", "end_turn")
    result.pending_approval = kwargs.get("pending_approval", None)
    result.last_input_tokens = kwargs.get("last_input_tokens", 0)
    return result


def _make_png(path: Path) -> Path:
    """Create a minimal 1x1 PNG file."""
    from PIL import Image

    img = Image.new("RGB", (1, 1), color="red")
    img.save(path, format="PNG")
    return path


class TestMediaServicesInit:
    """Verify media services are initialized on ChatServer."""

    def test_media_services_created(self, tmp_path) -> None:
        server = ChatServer(_make_agent_def(tmp_path))
        assert server._media_store is not None
        assert server._transcription is not None
        assert server._vision is not None


class TestProcessAttachments:
    """Unit tests for ChatServer._process_attachments."""

    def _server(self, tmp_path):
        return ChatServer(_make_agent_def(tmp_path))

    def test_no_attachments_passthrough(self, tmp_path) -> None:
        server = self._server(tmp_path)
        text, blocks = server._process_attachments("hello", None, "user1")
        assert text == "hello"
        assert blocks == []

    def test_empty_attachments_passthrough(self, tmp_path) -> None:
        server = self._server(tmp_path)
        text, blocks = server._process_attachments("hello", [], "user1")
        assert text == "hello"
        assert blocks == []

    def test_voice_attachment_transcribed(self, tmp_path) -> None:
        server = self._server(tmp_path)
        audio_file = tmp_path / "voice.ogg"
        audio_file.write_bytes(b"\x00" * 100)

        attachment = Attachment(
            type=AttachmentType.VOICE,
            file_path=audio_file,
            mime_type="audio/ogg",
        )

        with patch.object(server._transcription, "transcribe", return_value="Hello world"):
            text, blocks = server._process_attachments("some text", [attachment], "user1")

        assert "[Voice message]: Hello world" in text
        assert "some text" in text
        assert blocks == []

    def test_voice_transcription_failure(self, tmp_path) -> None:
        server = self._server(tmp_path)
        audio_file = tmp_path / "voice.ogg"
        audio_file.write_bytes(b"\x00" * 100)

        attachment = Attachment(
            type=AttachmentType.VOICE,
            file_path=audio_file,
            mime_type="audio/ogg",
        )

        with patch.object(server._transcription, "transcribe", return_value=""):
            text, blocks = server._process_attachments("", [attachment], "user1")

        assert "[Voice message: transcription failed]" in text

    def test_image_attachment_produces_content_block(self, tmp_path) -> None:
        server = self._server(tmp_path)
        img_file = tmp_path / "photo.png"
        _make_png(img_file)

        attachment = Attachment(
            type=AttachmentType.IMAGE,
            file_path=img_file,
            mime_type="image/png",
        )

        mock_block = {"type": "image", "source": {"type": "base64", "data": "abc"}}
        with patch.object(server._vision, "prepare_image", return_value=mock_block):
            text, blocks = server._process_attachments("what is this?", [attachment], "user1")

        assert text == "what is this?"
        assert len(blocks) == 1
        assert blocks[0]["type"] == "image"

    def test_image_processing_failure_no_block(self, tmp_path) -> None:
        server = self._server(tmp_path)
        img_file = tmp_path / "photo.png"
        _make_png(img_file)

        attachment = Attachment(
            type=AttachmentType.IMAGE,
            file_path=img_file,
            mime_type="image/png",
        )

        with patch.object(server._vision, "prepare_image", return_value=None):
            text, blocks = server._process_attachments("look at this", [attachment], "user1")

        assert text == "look at this"
        assert blocks == []

    def test_voice_and_image_combined(self, tmp_path) -> None:
        server = self._server(tmp_path)

        audio_file = tmp_path / "voice.ogg"
        audio_file.write_bytes(b"\x00" * 100)
        img_file = tmp_path / "photo.png"
        _make_png(img_file)

        voice_att = Attachment(
            type=AttachmentType.VOICE,
            file_path=audio_file,
            mime_type="audio/ogg",
        )
        image_att = Attachment(
            type=AttachmentType.IMAGE,
            file_path=img_file,
            mime_type="image/png",
        )

        mock_block = {"type": "image", "source": {"type": "base64", "data": "abc"}}
        with (
            patch.object(server._transcription, "transcribe", return_value="describe this"),
            patch.object(server._vision, "prepare_image", return_value=mock_block),
        ):
            text, blocks = server._process_attachments("", [voice_att, image_att], "user1")

        assert "[Voice message]: describe this" in text
        assert len(blocks) == 1
        assert blocks[0]["type"] == "image"

    def test_multiple_images(self, tmp_path) -> None:
        server = self._server(tmp_path)

        attachments = []
        for i in range(3):
            img_file = tmp_path / f"photo{i}.png"
            _make_png(img_file)
            attachments.append(
                Attachment(
                    type=AttachmentType.IMAGE,
                    file_path=img_file,
                    mime_type="image/png",
                )
            )

        mock_block = {"type": "image", "source": {"type": "base64", "data": "abc"}}
        with patch.object(server._vision, "prepare_image", return_value=mock_block):
            text, blocks = server._process_attachments("compare these", attachments, "user1")

        assert text == "compare these"
        assert len(blocks) == 3

    def test_voice_only_no_text(self, tmp_path) -> None:
        """Voice message with no accompanying text."""
        server = self._server(tmp_path)
        audio_file = tmp_path / "voice.ogg"
        audio_file.write_bytes(b"\x00" * 100)

        attachment = Attachment(
            type=AttachmentType.VOICE,
            file_path=audio_file,
            mime_type="audio/ogg",
        )

        with patch.object(server._transcription, "transcribe", return_value="Hello world"):
            text, blocks = server._process_attachments("", [attachment], "user1")

        assert text == "[Voice message]: Hello world"
        assert blocks == []

    def test_audio_type_also_transcribed(self, tmp_path) -> None:
        """AttachmentType.AUDIO should also be transcribed."""
        server = self._server(tmp_path)
        audio_file = tmp_path / "audio.mp3"
        audio_file.write_bytes(b"\x00" * 100)

        attachment = Attachment(
            type=AttachmentType.AUDIO,
            file_path=audio_file,
            mime_type="audio/mpeg",
        )

        with patch.object(server._transcription, "transcribe", return_value="Audio content"):
            text, blocks = server._process_attachments("", [attachment], "user1")

        assert "[Voice message]: Audio content" in text

    def test_voice_save_failure(self, tmp_path) -> None:
        """When media store fails to save, produce a failure notice."""
        server = self._server(tmp_path)
        attachment = Attachment(
            type=AttachmentType.VOICE,
            data=b"\x00" * 100,
            mime_type="audio/ogg",
        )

        with patch.object(
            server._media_store,
            "save_media",
            side_effect=ValueError("save failed"),
        ):
            text, blocks = server._process_attachments("hi", [attachment], "user1")

        assert "[Voice message: could not save audio file]" in text


class TestHandleMessageWithAttachments:
    """Integration tests: handle_message with attachments end-to-end."""

    def _server(self, tmp_path):
        return ChatServer(_make_agent_def(tmp_path))

    def test_image_message_uses_content_blocks(self, tmp_path) -> None:
        """Image attachments should produce content block user messages."""
        server = self._server(tmp_path)
        img_file = tmp_path / "photo.png"
        _make_png(img_file)

        attachment = Attachment(
            type=AttachmentType.IMAGE,
            file_path=img_file,
            mime_type="image/png",
        )

        mock_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
        }
        mock_result = _make_agent_result("I see a red pixel!")

        with (
            patch.object(server._vision, "prepare_image", return_value=mock_block),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            result = server.handle_message("user1", "What is this?", attachments=[attachment])

        assert result == "I see a red pixel!"
        # Verify the messages passed to run_agent_loop contain content blocks
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]  # positional arg
        # Find the user message
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) >= 1
        last_user = user_msgs[-1]
        # Content should be a list of blocks, not a string
        assert isinstance(last_user["content"], list)
        # Should have text + image blocks
        types = [b["type"] for b in last_user["content"]]
        assert "text" in types
        assert "image" in types

    def test_voice_message_prepends_transcription(self, tmp_path) -> None:
        """Voice attachment text should be prepended to user message."""
        server = self._server(tmp_path)
        audio_file = tmp_path / "voice.ogg"
        audio_file.write_bytes(b"\x00" * 100)

        attachment = Attachment(
            type=AttachmentType.VOICE,
            file_path=audio_file,
            mime_type="audio/ogg",
        )

        mock_result = _make_agent_result("Got it!")

        with (
            patch.object(server._transcription, "transcribe", return_value="Turn off the lights"),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            result = server.handle_message("user1", "", attachments=[attachment])

        assert result == "Got it!"
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        # Voice-only: content should be a plain string (no image blocks)
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: Turn off the lights" in last_user["content"]

    def test_text_only_message_unchanged(self, tmp_path) -> None:
        """Text-only messages (no attachments) still work as strings."""
        server = self._server(tmp_path)
        mock_result = _make_agent_result("Hello!")

        with patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop:
            result = server.handle_message("user1", "Hi there")

        assert result == "Hello!"
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert last_user["content"] == "Hi there"

    def test_voice_plus_image_combined(self, tmp_path) -> None:
        """Both voice and image in same message: transcription + vision."""
        server = self._server(tmp_path)
        audio_file = tmp_path / "voice.ogg"
        audio_file.write_bytes(b"\x00" * 100)
        img_file = tmp_path / "photo.png"
        _make_png(img_file)

        voice_att = Attachment(
            type=AttachmentType.VOICE,
            file_path=audio_file,
            mime_type="audio/ogg",
        )
        image_att = Attachment(
            type=AttachmentType.IMAGE,
            file_path=img_file,
            mime_type="image/png",
        )

        mock_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
        }
        mock_result = _make_agent_result("Got voice and image!")

        with (
            patch.object(server._transcription, "transcribe", return_value="What is this?"),
            patch.object(server._vision, "prepare_image", return_value=mock_block),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            result = server.handle_message("user1", "", attachments=[voice_att, image_att])

        assert result == "Got voice and image!"
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        # Should have content blocks (image present)
        assert isinstance(last_user["content"], list)
        types = [b["type"] for b in last_user["content"]]
        assert "text" in types
        assert "image" in types
        # Text block should contain transcription
        text_block = next(b for b in last_user["content"] if b["type"] == "text")
        assert "[Voice message]: What is this?" in text_block["text"]
