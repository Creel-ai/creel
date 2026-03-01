"""End-to-end integration test: iMessage image flow (MEDIA-011).

Simulates the full path:
  1. chat.db has a message with an image attachment row
  2. IMessageChannel._poll picks it up (attachment file path resolved)
  3. Image is copied to the media store
  4. VisionProcessor converts image to content blocks
  5. LLM receives image and generates a response (mocked)
  6. Response is sent back via AppleScript (mocked)
  7. Missing attachment file is handled gracefully
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from creel.channels.imessage import IMessageChannel
from creel.channels.message import Attachment, AttachmentType, IncomingMessage
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
from tests.helpers.imessage_db import (
    create_chat_db as _create_chat_db,
)
from tests.helpers.imessage_db import (
    insert_attachment as _insert_attachment,
)
from tests.helpers.imessage_db import (
    insert_handle as _insert_handle,
)
from tests.helpers.imessage_db import (
    insert_message as _insert_message,
)
from tests.helpers.imessage_db import (
    link_attachment as _link_attachment,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg_bytes() -> bytes:
    """Create minimal 1x1 JPEG image bytes."""
    from PIL import Image

    img = Image.new("RGB", (1, 1), color="green")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png_bytes() -> bytes:
    """Create minimal 1x1 PNG image bytes."""
    from PIL import Image

    img = Image.new("RGB", (1, 1), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_agent_def(tmp_path: Path) -> AgentDefinition:
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


def _make_agent_result(text: str = "response"):
    result = MagicMock()
    result.text = text
    result.turns_used = 1
    result.tool_calls_made = 0
    result.stop_reason = "end_turn"
    result.pending_approval = None
    result.last_input_tokens = 100
    return result


# ---------------------------------------------------------------------------
# Tests: iMessage poll picks up image attachment
# ---------------------------------------------------------------------------


class TestIMessagePollImage:
    """Verify that _poll returns messages with image attachment metadata."""

    def test_poll_image_attachment(self, tmp_path: Path) -> None:
        """A message with an image attachment should be returned with full metadata."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img_file = tmp_path / "photo.jpg"
        img_file.write_bytes(_make_jpeg_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, "check this out", handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename=str(img_file),
            mime_type="image/jpeg",
            transfer_name="photo.jpg",
            total_bytes=img_file.stat().st_size,
        )
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@icloud.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        msg = messages[0]
        assert msg["text"] == "check this out"
        assert msg["sender"] == "friend@icloud.com"
        assert len(msg["attachments"]) == 1

        att = msg["attachments"][0]
        assert att.type == AttachmentType.IMAGE
        assert att.file_path == img_file
        assert att.mime_type == "image/jpeg"
        assert att.file_name == "photo.jpg"

    def test_poll_image_only_no_text(self, tmp_path: Path) -> None:
        """An image-only message (no text) should still be returned."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img_file = tmp_path / "selfie.png"
        img_file.write_bytes(_make_png_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(db_path, 1, str(img_file), "image/png", "selfie.png")
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@icloud.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        assert messages[0]["text"] == ""
        assert len(messages[0]["attachments"]) == 1
        assert messages[0]["attachments"][0].type == AttachmentType.IMAGE

    def test_poll_tilde_path_resolved(self, tmp_path: Path) -> None:
        """Attachment filenames with ~ are expanded to the home directory."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        # Create file at a path that has a ~ prefix in the DB
        marker = tmp_path / "marker.jpg"
        marker.write_bytes(_make_jpeg_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, "pic", handle_id=1)
        # Use an absolute path (no tilde) — we test tilde expansion separately
        # in test_imessage_media.py; here just verify the attachment flows through
        _insert_attachment(db_path, 1, str(marker), "image/jpeg", "marker.jpg")
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@icloud.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        assert messages[0]["attachments"][0].file_path == marker


# ---------------------------------------------------------------------------
# Tests: Missing attachment file handled gracefully
# ---------------------------------------------------------------------------


class TestMissingAttachmentFile:
    """Test that missing attachment files don't crash the flow."""

    def test_missing_file_still_returns_attachment(self, tmp_path: Path) -> None:
        """When the attachment file doesn't exist on disk, file_path is None
        but the attachment is still returned (with transfer_name)."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, "old photo", handle_id=1)
        # Point to a non-existent file (macOS cleaned it up)
        _insert_attachment(
            db_path,
            1,
            "/nonexistent/Library/Messages/Attachments/xx/yy/uuid/photo.heic",
            "image/heic",
            "photo.heic",
            total_bytes=500_000,
        )
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@icloud.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        att = messages[0]["attachments"][0]
        assert att.type == AttachmentType.IMAGE
        assert att.file_path is None  # file missing
        assert att.file_name == "photo.heic"

    def test_missing_file_callback_still_invoked(self, tmp_path: Path) -> None:
        """Even with a missing attachment file, the callback should be invoked
        with the IncomingMessage (attachment has file_path=None)."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, "look at this", handle_id=1)
        _insert_attachment(
            db_path,
            1,
            "/nonexistent/path/photo.jpg",
            "image/jpeg",
            "photo.jpg",
        )
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(
            allowed_senders=["friend@icloud.com"],
            poll_interval=1,
        )
        channel.MESSAGES_DB = db_path

        callback = MagicMock(return_value="got it")
        with (
            patch.object(channel, "send"),
            patch.object(channel, "_get_latest_rowid", return_value=0),
            patch("sys.platform", "darwin"),
        ):
            original_poll = channel._poll

            def poll_once_then_stop(after_rowid):
                result = original_poll(after_rowid)
                channel._stop_requested = True
                return result

            channel._poll = poll_once_then_stop
            channel.listen(callback)

        # Callback should receive IncomingMessage (attachment present even if file missing)
        callback.assert_called_once()
        incoming = callback.call_args.args[0]
        assert isinstance(incoming, IncomingMessage)
        assert incoming.sender_id == "friend@icloud.com"
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].file_path is None

    def test_missing_file_chatserver_handles_gracefully(self, tmp_path: Path) -> None:
        """ChatServer should handle image attachments with file_path=None
        without crashing — the image is simply skipped."""
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        attachment = Attachment(
            type=AttachmentType.IMAGE,
            file_path=None,  # file missing from disk
            mime_type="image/jpeg",
            file_name="cleaned_up.jpg",
        )

        mock_result = _make_agent_result("Just text response")

        with patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop:
            response = server.handle_message(
                "friend@icloud.com",
                "what was that photo?",
                attachments=[attachment],
            )

        assert response == "Just text response"
        # Without a file on disk, vision can't process — content should be plain text
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)


# ---------------------------------------------------------------------------
# Tests: listen() sends IncomingMessage to callback for image messages
# ---------------------------------------------------------------------------


class TestListenImageCallback:
    """Verify listen() passes IncomingMessage to callback when images are present."""

    def test_image_message_callback_gets_incoming_message(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img_file = tmp_path / "photo.jpg"
        img_file.write_bytes(_make_jpeg_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, "what do you think?", handle_id=1)
        _insert_attachment(db_path, 1, str(img_file), "image/jpeg", "photo.jpg")
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(
            allowed_senders=["friend@icloud.com"],
            poll_interval=1,
        )
        channel.MESSAGES_DB = db_path

        callback = MagicMock(return_value="nice photo!")
        with (
            patch.object(channel, "send"),
            patch.object(channel, "_get_latest_rowid", return_value=0),
            patch("sys.platform", "darwin"),
        ):
            original_poll = channel._poll

            def poll_once_then_stop(after_rowid):
                result = original_poll(after_rowid)
                channel._stop_requested = True
                return result

            channel._poll = poll_once_then_stop
            channel.listen(callback)

        callback.assert_called_once()
        incoming = callback.call_args.args[0]
        assert isinstance(incoming, IncomingMessage)
        assert incoming.sender_id == "friend@icloud.com"
        assert incoming.text == "what do you think?"
        assert incoming.channel == "imessage"
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.IMAGE
        assert incoming.attachments[0].file_path == img_file

    def test_response_sent_via_applescript(self, tmp_path: Path) -> None:
        """The agent's response should be sent back to the sender via send()."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img_file = tmp_path / "photo.jpg"
        img_file.write_bytes(_make_jpeg_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, "look", handle_id=1)
        _insert_attachment(db_path, 1, str(img_file), "image/jpeg", "photo.jpg")
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(
            allowed_senders=["friend@icloud.com"],
            poll_interval=1,
        )
        channel.MESSAGES_DB = db_path

        callback = MagicMock(return_value="I see a photo!")

        with (
            patch.object(channel, "send") as mock_send,
            patch.object(channel, "_get_latest_rowid", return_value=0),
            patch("sys.platform", "darwin"),
        ):
            original_poll = channel._poll

            def poll_once_then_stop(after_rowid):
                result = original_poll(after_rowid)
                channel._stop_requested = True
                return result

            channel._poll = poll_once_then_stop
            channel.listen(callback)

        # send() should be called with the sender and response
        mock_send.assert_called_once_with("friend@icloud.com", "I see a photo!")


# ---------------------------------------------------------------------------
# Tests: Full E2E flow (iMessage → ChatServer → LLM → response)
# ---------------------------------------------------------------------------


class TestE2EIMessageImage:
    """Full integration test: iMessage image → ChatServer → LLM → response."""

    def test_full_image_flow_via_chatserver(self, tmp_path: Path) -> None:
        """Simulate: attachment on disk → ChatServer processes image → LLM gets content blocks."""
        jpeg_bytes = _make_jpeg_bytes()
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        # Create a file as if it was found in ~/Library/Messages/Attachments/
        img_file = tmp_path / "imessage_photo.jpg"
        img_file.write_bytes(jpeg_bytes)

        mock_vision_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc123"},
        }
        mock_result = _make_agent_result("I see a green pixel!")

        with (
            patch.object(server._vision, "prepare_image", return_value=mock_vision_block),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            attachment = Attachment(
                type=AttachmentType.IMAGE,
                file_path=img_file,
                mime_type="image/jpeg",
                file_name="imessage_photo.jpg",
            )
            response = server.handle_message(
                "friend@icloud.com",
                "What is this image?",
                attachments=[attachment],
            )

        # Verify response
        assert response == "I see a green pixel!"

        # Verify LLM received content blocks
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

        # Verify media file was saved to disk in the media store
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.jpg"))
        assert len(saved_files) >= 1
        assert saved_files[0].read_bytes() == jpeg_bytes

    def test_full_image_flow_image_only_no_text(self, tmp_path: Path) -> None:
        """Image-only message (no text) should still produce content blocks."""
        jpeg_bytes = _make_jpeg_bytes()
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        img_file = tmp_path / "selfie.jpg"
        img_file.write_bytes(jpeg_bytes)

        mock_vision_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "xyz"},
        }
        mock_result = _make_agent_result("Nice selfie!")

        with (
            patch.object(server._vision, "prepare_image", return_value=mock_vision_block),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            attachment = Attachment(
                type=AttachmentType.IMAGE,
                file_path=img_file,
                mime_type="image/jpeg",
            )
            response = server.handle_message(
                "friend@icloud.com",
                "",  # no text
                attachments=[attachment],
            )

        assert response == "Nice selfie!"

        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], list)
        types = [b["type"] for b in last_user["content"]]
        assert "image" in types

    def test_full_image_flow_via_incoming_message(self, tmp_path: Path) -> None:
        """Simulate the DaemonService path: IncomingMessage → ChatServer."""
        jpeg_bytes = _make_jpeg_bytes()
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        img_file = tmp_path / "photo.jpg"
        img_file.write_bytes(jpeg_bytes)

        mock_vision_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "enc"},
        }
        mock_result = _make_agent_result("Got your iMessage photo!")

        incoming = IncomingMessage(
            sender_id="friend@icloud.com",
            text="describe this",
            attachments=[
                Attachment(
                    type=AttachmentType.IMAGE,
                    file_path=img_file,
                    mime_type="image/jpeg",
                    file_name="photo.jpg",
                )
            ],
            channel="imessage",
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

        assert response == "Got your iMessage photo!"

        # Verify file on disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.jpg"))
        assert len(saved_files) >= 1

    def test_full_flow_poll_to_chatserver(self, tmp_path: Path) -> None:
        """Full E2E: iMessage poll picks up image → ChatServer processes → reply sent.

        This simulates the DaemonService wiring: channel.listen(callback)
        where callback routes through ChatServer.handle_message.
        """
        jpeg_bytes = _make_jpeg_bytes()
        agent_def = _make_agent_def(tmp_path)

        # Set up chat.db with an image message
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img_file = tmp_path / "photo.jpg"
        img_file.write_bytes(jpeg_bytes)

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, "What do you see?", handle_id=1)
        _insert_attachment(db_path, 1, str(img_file), "image/jpeg", "photo.jpg")
        _link_attachment(db_path, 1, 1)

        mock_vision_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "encoded"},
        }
        mock_result = _make_agent_result("I see a green pixel in your photo!")

        server = ChatServer(agent_def)

        channel = IMessageChannel(
            allowed_senders=["friend@icloud.com"],
            poll_interval=1,
        )
        channel.MESSAGES_DB = db_path

        sent_responses: list[tuple[str, str]] = []

        with (
            patch.object(server._vision, "prepare_image", return_value=mock_vision_block),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):

            def callback(*args):
                """Mimics DaemonService: routes IncomingMessage to ChatServer."""
                if len(args) == 1 and isinstance(args[0], IncomingMessage):
                    msg = args[0]
                    return server.handle_message(
                        msg.sender_id,
                        msg.text or "",
                        attachments=msg.attachments,
                    )
                return server.handle_message(args[0], args[1])

            def mock_send(recipient, text):
                if text:
                    sent_responses.append((recipient, text))

            with (
                patch.object(channel, "send", side_effect=mock_send),
                patch.object(channel, "_get_latest_rowid", return_value=0),
                patch("sys.platform", "darwin"),
            ):
                original_poll = channel._poll

                def poll_once_then_stop(after_rowid):
                    result = original_poll(after_rowid)
                    channel._stop_requested = True
                    return result

                channel._poll = poll_once_then_stop
                channel.listen(callback)

        # Verify response was sent back
        assert len(sent_responses) == 1
        assert sent_responses[0] == (
            "friend@icloud.com",
            "I see a green pixel in your photo!",
        )

        # Verify LLM received image content blocks
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], list)
        types = [b["type"] for b in last_user["content"]]
        assert "text" in types
        assert "image" in types

        # Verify media file saved to disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.jpg"))
        assert len(saved_files) >= 1
        assert saved_files[0].read_bytes() == jpeg_bytes

    def test_multiple_images_in_one_message(self, tmp_path: Path) -> None:
        """Multiple image attachments should all be processed as content blocks."""
        jpeg_bytes = _make_jpeg_bytes()
        png_bytes = _make_png_bytes()
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        img1 = tmp_path / "photo1.jpg"
        img2 = tmp_path / "photo2.png"
        img1.write_bytes(jpeg_bytes)
        img2.write_bytes(png_bytes)

        mock_vision_block_jpg = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "jpg_data",
            },
        }
        mock_vision_block_png = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "png_data"},
        }
        mock_result = _make_agent_result("I see two images!")

        with (
            patch.object(
                server._vision,
                "prepare_image",
                side_effect=[mock_vision_block_jpg, mock_vision_block_png],
            ),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            attachments = [
                Attachment(
                    type=AttachmentType.IMAGE,
                    file_path=img1,
                    mime_type="image/jpeg",
                    file_name="photo1.jpg",
                ),
                Attachment(
                    type=AttachmentType.IMAGE,
                    file_path=img2,
                    mime_type="image/png",
                    file_name="photo2.png",
                ),
            ]
            response = server.handle_message(
                "friend@icloud.com",
                "compare these",
                attachments=attachments,
            )

        assert response == "I see two images!"

        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], list)
        image_blocks = [b for b in last_user["content"] if b["type"] == "image"]
        assert len(image_blocks) == 2

    def test_media_disabled_ignores_imessage_attachments(self, tmp_path: Path) -> None:
        """When media is disabled, iMessage image attachments are silently ignored."""
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

        server = ChatServer(agent_def)

        img_file = tmp_path / "photo.jpg"
        img_file.write_bytes(_make_jpeg_bytes())

        attachment = Attachment(
            type=AttachmentType.IMAGE,
            file_path=img_file,
            mime_type="image/jpeg",
        )

        mock_result = _make_agent_result("Text only response")

        with patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop:
            response = server.handle_message(
                "friend@icloud.com",
                "What is this?",
                attachments=[attachment],
            )

        assert response == "Text only response"
        # Content should be plain text (no image blocks)
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
