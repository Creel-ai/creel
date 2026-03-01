"""Tests for iMessage channel media attachment support (MEDIA-006)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from taskrunner.channels.imessage import IMessageChannel
from taskrunner.channels.message import Attachment, AttachmentType, IncomingMessage
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
# Tests for _query_attachments
# ---------------------------------------------------------------------------


class TestQueryAttachments:
    """Test the SQL query that fetches attachments for a message."""

    def test_no_attachments(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)
        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "hello", handle_id=1)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert attachments == []

    def test_image_attachment_with_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        # Create a fake image file on disk
        img_file = tmp_path / "photo.jpg"
        img_file.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "check this out", handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename=str(img_file),
            mime_type="image/jpeg",
            transfer_name="photo.jpg",
            total_bytes=len(b"\xff\xd8\xff\xe0fake-jpeg"),
        )
        _link_attachment(db_path, message_id=1, attachment_id=1)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert len(attachments) == 1
        att = attachments[0]
        assert att.type == AttachmentType.IMAGE
        assert att.file_path == img_file
        assert att.mime_type == "image/jpeg"
        assert att.file_name == "photo.jpg"
        assert att.file_size == len(b"\xff\xd8\xff\xe0fake-jpeg")

    def test_voice_attachment_caf(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(b"caff-audio-data")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename=str(caf_file),
            mime_type="audio/x-caf",
            transfer_name="voice.caf",
        )
        _link_attachment(db_path, message_id=1, attachment_id=1)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert len(attachments) == 1
        assert attachments[0].type == AttachmentType.VOICE
        assert attachments[0].mime_type == "audio/x-caf"

    def test_audio_attachment_not_voice(self, tmp_path: Path) -> None:
        """Non-voice audio (e.g. shared song) should be AttachmentType.AUDIO."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        mp3_file = tmp_path / "song.mp3"
        mp3_file.write_bytes(b"fake-mp3")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename=str(mp3_file),
            mime_type="audio/mpeg",
            transfer_name="song.mp3",
        )
        _link_attachment(db_path, message_id=1, attachment_id=1)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert len(attachments) == 1
        assert attachments[0].type == AttachmentType.AUDIO

    def test_video_attachment(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        vid_file = tmp_path / "clip.mp4"
        vid_file.write_bytes(b"fake-mp4")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename=str(vid_file),
            mime_type="video/mp4",
            transfer_name="clip.mp4",
        )
        _link_attachment(db_path, message_id=1, attachment_id=1)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert len(attachments) == 1
        assert attachments[0].type == AttachmentType.VIDEO

    def test_missing_file_on_disk(self, tmp_path: Path) -> None:
        """When the attachment file is missing, file_path should be None but attachment is still returned."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "look", handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename="/nonexistent/path/photo.jpg",
            mime_type="image/jpeg",
            transfer_name="photo.jpg",
        )
        _link_attachment(db_path, message_id=1, attachment_id=1)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert len(attachments) == 1
        assert attachments[0].file_path is None
        assert attachments[0].file_name == "photo.jpg"
        assert attachments[0].type == AttachmentType.IMAGE

    def test_tilde_expansion_in_filename(self, tmp_path: Path) -> None:
        """chat.db stores paths like ~/Library/Messages/Attachments/... — ~ must be expanded."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        # Create a file where ~ would expand to
        fake_home_file = Path.home() / ".creel_test_attachment_temp.jpg"
        try:
            fake_home_file.write_bytes(b"test-image")

            _insert_handle(db_path, 1, "friend@example.com")
            _insert_message(db_path, 1, None, handle_id=1)
            _insert_attachment(
                db_path,
                rowid=1,
                filename="~/.creel_test_attachment_temp.jpg",
                mime_type="image/jpeg",
                transfer_name="test.jpg",
            )
            _link_attachment(db_path, message_id=1, attachment_id=1)

            conn = sqlite3.connect(str(db_path))
            attachments = IMessageChannel._query_attachments(conn, 1)
            conn.close()

            assert len(attachments) == 1
            assert attachments[0].file_path == fake_home_file
        finally:
            fake_home_file.unlink(missing_ok=True)

    def test_multiple_attachments_on_one_message(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img1 = tmp_path / "a.jpg"
        img2 = tmp_path / "b.png"
        img1.write_bytes(b"img1")
        img2.write_bytes(b"img2")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "two pics", handle_id=1)
        _insert_attachment(db_path, 1, str(img1), "image/jpeg", "a.jpg")
        _insert_attachment(db_path, 2, str(img2), "image/png", "b.png")
        _link_attachment(db_path, 1, 1)
        _link_attachment(db_path, 1, 2)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert len(attachments) == 2
        assert all(a.type == AttachmentType.IMAGE for a in attachments)

    def test_skip_attachment_with_no_file_and_no_transfer_name(self, tmp_path: Path) -> None:
        """Attachments with neither a file nor a transfer_name should be skipped."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "hi", handle_id=1)
        _insert_attachment(db_path, 1, None, None, None)
        _link_attachment(db_path, 1, 1)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert attachments == []

    def test_unknown_mime_type_defaults_to_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        doc = tmp_path / "doc.pdf"
        doc.write_bytes(b"pdf-bytes")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(db_path, 1, str(doc), "application/pdf", "doc.pdf")
        _link_attachment(db_path, 1, 1)

        conn = sqlite3.connect(str(db_path))
        attachments = IMessageChannel._query_attachments(conn, 1)
        conn.close()

        assert len(attachments) == 1
        assert attachments[0].type == AttachmentType.FILE


# ---------------------------------------------------------------------------
# Tests for _poll (messages + attachments)
# ---------------------------------------------------------------------------


class TestPollWithAttachments:
    """Test that _poll returns messages including attachment metadata."""

    def test_text_only_message(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)
        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "just text", handle_id=1)

        channel = IMessageChannel(allowed_senders=["friend@example.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        assert messages[0]["text"] == "just text"
        assert messages[0]["attachments"] == []

    def test_message_with_image_attachment(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"jpeg-data")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "look at this", handle_id=1)
        _insert_attachment(db_path, 1, str(img), "image/jpeg", "photo.jpg")
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@example.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        assert messages[0]["text"] == "look at this"
        assert len(messages[0]["attachments"]) == 1
        assert messages[0]["attachments"][0].type == AttachmentType.IMAGE

    def test_attachment_only_message_no_text(self, tmp_path: Path) -> None:
        """Messages with only an attachment (no text) should still be returned."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img = tmp_path / "selfie.jpg"
        img.write_bytes(b"jpeg-selfie")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(db_path, 1, str(img), "image/jpeg", "selfie.jpg")
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@example.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        assert messages[0]["text"] == ""
        assert len(messages[0]["attachments"]) == 1

    def test_respects_after_rowid(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)
        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "old", handle_id=1)
        _insert_message(db_path, 2, "new", handle_id=1)

        channel = IMessageChannel(allowed_senders=["friend@example.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(1)
        assert len(messages) == 1
        assert messages[0]["text"] == "new"

    def test_skips_is_from_me(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)
        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "my sent message", handle_id=1, is_from_me=1)

        channel = IMessageChannel(allowed_senders=["friend@example.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 0

    def test_no_duplicate_for_multi_attachment(self, tmp_path: Path) -> None:
        """A message with 2 attachments should appear once, not twice."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img1 = tmp_path / "a.jpg"
        img2 = tmp_path / "b.jpg"
        img1.write_bytes(b"a")
        img2.write_bytes(b"b")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "two pics", handle_id=1)
        _insert_attachment(db_path, 1, str(img1), "image/jpeg", "a.jpg")
        _insert_attachment(db_path, 2, str(img2), "image/jpeg", "b.jpg")
        _link_attachment(db_path, 1, 1)
        _link_attachment(db_path, 1, 2)

        channel = IMessageChannel(allowed_senders=["friend@example.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        assert len(messages[0]["attachments"]) == 2


# ---------------------------------------------------------------------------
# Tests for listen() callback behavior with attachments
# ---------------------------------------------------------------------------


class TestListenWithAttachments:
    """Test that listen() passes IncomingMessage to callback for media messages."""

    def _make_channel(self, db_path: Path) -> IMessageChannel:
        channel = IMessageChannel(allowed_senders=["friend@example.com"], poll_interval=1)
        channel.MESSAGES_DB = db_path
        return channel

    def test_text_only_calls_callback_with_two_args(self, tmp_path: Path) -> None:
        """Text-only messages should invoke callback(sender_id, text) as before."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)
        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "hello", handle_id=1)

        channel = self._make_channel(db_path)

        callback = MagicMock(return_value="hi back")
        # Patch send to avoid AppleScript, and stop after one poll
        with (
            patch.object(channel, "send"),
            patch.object(channel, "_get_latest_rowid", return_value=0),
            patch("sys.platform", "darwin"),
        ):
            # Run one iteration then stop
            def poll_once_then_stop(original_poll):
                """Run the real poll once, then request stop."""

                def wrapper(after_rowid):
                    result = original_poll(after_rowid)
                    channel._stop_requested = True
                    return result

                return wrapper

            original_poll = channel._poll
            channel._poll = poll_once_then_stop(original_poll)
            channel.listen(callback)

        # callback was called with (sender_id, text)
        callback.assert_called_once_with("friend@example.com", "hello")

    def test_attachment_message_calls_callback_with_incoming_message(self, tmp_path: Path) -> None:
        """Messages with attachments should invoke callback(IncomingMessage)."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"jpeg")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, "look", handle_id=1)
        _insert_attachment(db_path, 1, str(img), "image/jpeg", "photo.jpg")
        _link_attachment(db_path, 1, 1)

        channel = self._make_channel(db_path)

        callback = MagicMock(return_value="nice pic!")
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

        # callback should be called with a single IncomingMessage
        callback.assert_called_once()
        args = callback.call_args
        assert len(args.args) == 1
        incoming = args.args[0]
        assert isinstance(incoming, IncomingMessage)
        assert incoming.sender_id == "friend@example.com"
        assert incoming.text == "look"
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.IMAGE
        assert incoming.channel == "imessage"

    def test_attachment_only_no_text(self, tmp_path: Path) -> None:
        """Attachment-only messages pass IncomingMessage with text=None."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        img = tmp_path / "img.png"
        img.write_bytes(b"png")

        _insert_handle(db_path, 1, "friend@example.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(db_path, 1, str(img), "image/png", "img.png")
        _link_attachment(db_path, 1, 1)

        channel = self._make_channel(db_path)

        callback = MagicMock(return_value="got your pic")
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
        assert incoming.text is None
        assert len(incoming.attachments) == 1

    def test_own_message_skipped(self, tmp_path: Path) -> None:
        """Messages starting with MESSAGE_PREFIX should still be skipped."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)
        _insert_handle(db_path, 1, "friend@example.com")
        from taskrunner.outputs import MESSAGE_PREFIX

        _insert_message(db_path, 1, f"{MESSAGE_PREFIX} my reply", handle_id=1)

        channel = self._make_channel(db_path)

        callback = MagicMock(return_value="response")
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

        callback.assert_not_called()

    def test_disallowed_sender_skipped(self, tmp_path: Path) -> None:
        """Messages from non-allowed senders should be skipped."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)
        _insert_handle(db_path, 1, "stranger@example.com")
        _insert_message(db_path, 1, "hi", handle_id=1)

        channel = self._make_channel(db_path)

        callback = MagicMock(return_value="no")
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

        callback.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for DaemonService.send_message with IncomingMessage
# ---------------------------------------------------------------------------


class TestDaemonServiceIncomingMessage:
    """Test that DaemonService.send_message handles IncomingMessage."""

    def test_incoming_message_passes_attachments(self) -> None:
        """DaemonService should forward attachments to ChatServer.handle_message."""
        from taskrunner.daemon.service import DaemonService

        mock_server = MagicMock()
        mock_server.handle_message.return_value = "response"
        mock_server._session_mgr = MagicMock()

        service = DaemonService.__new__(DaemonService)
        service._server = mock_server
        service._lock = __import__("threading").RLock()

        attachment = Attachment(
            type=AttachmentType.IMAGE,
            file_path=Path("/tmp/test.jpg"),
            mime_type="image/jpeg",
        )
        incoming = IncomingMessage(
            sender_id="user1",
            text="look at this",
            attachments=[attachment],
            channel="imessage",
        )

        result = service.send_message(incoming)

        assert result == "response"
        mock_server.handle_message.assert_called_once_with(
            "user1",
            "look at this",
            auto_approve=False,
            attachments=[attachment],
            channel="imessage",
        )

    def test_plain_text_still_works(self) -> None:
        """DaemonService.send_message(sender_id, text) still works as before."""
        from taskrunner.daemon.service import DaemonService

        mock_server = MagicMock()
        mock_server.handle_message.return_value = "ok"
        mock_server._session_mgr = MagicMock()

        service = DaemonService.__new__(DaemonService)
        service._server = mock_server
        service._lock = __import__("threading").RLock()

        result = service.send_message("user1", "hello")

        assert result == "ok"
        mock_server.handle_message.assert_called_once_with("user1", "hello", auto_approve=False)

    def test_incoming_message_with_no_text(self) -> None:
        """IncomingMessage with text=None should pass empty string to handle_message."""
        from taskrunner.daemon.service import DaemonService

        mock_server = MagicMock()
        mock_server.handle_message.return_value = "response"
        mock_server._session_mgr = MagicMock()

        service = DaemonService.__new__(DaemonService)
        service._server = mock_server
        service._lock = __import__("threading").RLock()

        incoming = IncomingMessage(
            sender_id="user1",
            text=None,
            attachments=[Attachment(type=AttachmentType.VOICE, file_path=Path("/tmp/voice.caf"))],
        )

        result = service.send_message(incoming)

        assert result == "response"
        call_args = mock_server.handle_message.call_args
        assert call_args.args[1] == ""  # text should be empty string
