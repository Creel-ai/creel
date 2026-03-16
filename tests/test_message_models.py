"""Tests for IncomingMessage and Attachment models (MEDIA-001)."""

from __future__ import annotations

from pathlib import Path

from creel.channels.message import Attachment, AttachmentType, IncomingMessage


class TestAttachment:
    def test_image_attachment(self):
        att = Attachment(
            type=AttachmentType.IMAGE,
            file_path=Path("/tmp/photo.jpg"),
            mime_type="image/jpeg",
            file_size=1024,
        )
        assert att.type == AttachmentType.IMAGE
        assert att.file_path == Path("/tmp/photo.jpg")
        assert att.mime_type == "image/jpeg"
        assert att.file_size == 1024

    def test_voice_attachment(self):
        att = Attachment(
            type=AttachmentType.VOICE,
            file_path=Path("/tmp/voice.oga"),
            mime_type="audio/ogg",
        )
        assert att.type == AttachmentType.VOICE

    def test_attachment_with_data(self):
        att = Attachment(
            type=AttachmentType.IMAGE,
            data=b"\x89PNG\r\n",
            mime_type="image/png",
        )
        assert att.data == b"\x89PNG\r\n"

    def test_attachment_with_url(self):
        att = Attachment(
            type=AttachmentType.FILE,
            url="https://example.com/doc.pdf",
            file_name="doc.pdf",
        )
        assert att.url == "https://example.com/doc.pdf"
        assert att.file_name == "doc.pdf"

    def test_attachment_defaults(self):
        att = Attachment(type=AttachmentType.IMAGE)
        assert att.file_path is None
        assert att.url is None
        assert att.mime_type is None
        assert att.file_name is None
        assert att.file_size is None
        assert att.data is None

    def test_attachment_type_values(self):
        assert AttachmentType.IMAGE == "image"
        assert AttachmentType.VOICE == "voice"
        assert AttachmentType.AUDIO == "audio"
        assert AttachmentType.VIDEO == "video"
        assert AttachmentType.FILE == "file"


class TestIncomingMessage:
    def test_text_only_message(self):
        msg = IncomingMessage(sender_id="user1", text="Hello")
        assert msg.sender_id == "user1"
        assert msg.text == "Hello"
        assert msg.attachments == []
        assert msg.channel is None

    def test_message_with_attachments(self):
        att = Attachment(type=AttachmentType.IMAGE, file_path=Path("/tmp/img.jpg"))
        msg = IncomingMessage(
            sender_id="user1",
            text="Check this out",
            attachments=[att],
            channel="telegram",
        )
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == AttachmentType.IMAGE
        assert msg.channel == "telegram"

    def test_message_with_no_text(self):
        att = Attachment(type=AttachmentType.IMAGE, file_path=Path("/tmp/img.jpg"))
        msg = IncomingMessage(sender_id="user1", attachments=[att])
        assert msg.text is None
        assert len(msg.attachments) == 1

    def test_message_multiple_attachments(self):
        atts = [
            Attachment(type=AttachmentType.IMAGE, file_path=Path("/tmp/a.jpg")),
            Attachment(type=AttachmentType.VOICE, file_path=Path("/tmp/b.oga")),
        ]
        msg = IncomingMessage(sender_id="user1", text="hi", attachments=atts)
        assert len(msg.attachments) == 2
        assert msg.attachments[0].type == AttachmentType.IMAGE
        assert msg.attachments[1].type == AttachmentType.VOICE


class TestChatServerAttachmentsParam:
    def test_handle_message_accepts_attachments(self, tmp_path):
        """ChatServer.handle_message accepts attachments keyword arg."""
        from unittest.mock import MagicMock, patch

        from creel.chat import ChatServer
        from creel.models import (
            AgentConfig,
            AgentDefinition,
            ChannelsConfig,
            LLMConfig,
            SessionConfig,
            WorkspaceConfig,
        )

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        agent_def = AgentDefinition(
            system_prompt="Test.",
            llm=LLMConfig(model="test-model", max_tokens=100),
            agent=AgentConfig(max_turns=3),
            session=SessionConfig(
                sessions_dir=str(sessions_dir),
                summarize_on_trim=False,
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "ws")),
            channels=ChannelsConfig(),
        )
        server = ChatServer(agent_def)

        mock_result = MagicMock()
        mock_result.text = "response"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approvals = []
        mock_result.last_input_tokens = 0

        att = Attachment(type=AttachmentType.IMAGE, file_path=Path("/tmp/test.jpg"))

        with patch("creel.chat.run_agent_loop", return_value=mock_result):
            # Should not raise when attachments is passed
            result = server.handle_message("user1", "describe this", attachments=[att])
        assert result == "response"

    def test_handle_message_works_without_attachments(self, tmp_path):
        """Backward compat: handle_message works without attachments arg."""
        from unittest.mock import MagicMock, patch

        from creel.chat import ChatServer
        from creel.models import (
            AgentConfig,
            AgentDefinition,
            ChannelsConfig,
            LLMConfig,
            SessionConfig,
            WorkspaceConfig,
        )

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        agent_def = AgentDefinition(
            system_prompt="Test.",
            llm=LLMConfig(model="test-model", max_tokens=100),
            agent=AgentConfig(max_turns=3),
            session=SessionConfig(
                sessions_dir=str(sessions_dir),
                summarize_on_trim=False,
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "ws")),
            channels=ChannelsConfig(),
        )
        server = ChatServer(agent_def)

        mock_result = MagicMock()
        mock_result.text = "response"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"
        mock_result.pending_approvals = []
        mock_result.last_input_tokens = 0

        with patch("creel.chat.run_agent_loop", return_value=mock_result):
            result = server.handle_message("user1", "hello")
        assert result == "response"
