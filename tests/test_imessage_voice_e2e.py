"""End-to-end integration test: iMessage voice flow (MEDIA-012).

Simulates the full path:
  1. chat.db has a message with a voice/audio attachment (.caf format)
  2. IMessageChannel._poll picks it up (attachment file path resolved)
  3. Audio file is copied to the media store
  4. TranscriptionService transcribes the audio (mocked Whisper API)
     — .caf is not natively supported by Whisper, so ffmpeg conversion is tested
  5. Transcribed text is prepended to the user message
  6. LLM receives the text and generates a response (mocked)
  7. Response is sent back via AppleScript (mocked)
"""

from __future__ import annotations

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


def _make_caf_bytes() -> bytes:
    """Create minimal CAF (Core Audio Format) header bytes for testing.

    Not a valid audio file, but sufficient for testing the pipeline
    where actual decoding is mocked.
    """
    # CAF magic: "caff" followed by version and flags
    return b"caff\x00\x01\x00\x00" + b"\x00" * 100


def _make_m4a_bytes() -> bytes:
    """Create minimal M4A header bytes for testing."""
    return b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100


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
    result.pending_approvals = []
    result.last_input_tokens = 100
    return result


# ---------------------------------------------------------------------------
# Tests: iMessage poll picks up voice attachment (.caf)
# ---------------------------------------------------------------------------


class TestIMessagePollVoice:
    """Verify that _poll returns messages with voice attachment metadata."""

    def test_poll_voice_caf_attachment(self, tmp_path: Path) -> None:
        """A message with a .caf voice attachment should be returned as VOICE type."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(_make_caf_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename=str(caf_file),
            mime_type="audio/x-caf",
            transfer_name="voice.caf",
            total_bytes=caf_file.stat().st_size,
        )
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@icloud.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        msg = messages[0]
        assert msg["text"] == ""
        assert msg["sender"] == "friend@icloud.com"
        assert len(msg["attachments"]) == 1

        att = msg["attachments"][0]
        assert att.type == AttachmentType.VOICE
        assert att.file_path == caf_file
        assert att.mime_type == "audio/x-caf"
        assert att.file_name == "voice.caf"

    def test_poll_voice_with_text(self, tmp_path: Path) -> None:
        """A voice message with accompanying text should have both."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        caf_file = tmp_path / "note.caf"
        caf_file.write_bytes(_make_caf_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, "listen to this", handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename=str(caf_file),
            mime_type="audio/x-caf",
            transfer_name="note.caf",
        )
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@icloud.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        assert messages[0]["text"] == "listen to this"
        assert len(messages[0]["attachments"]) == 1
        assert messages[0]["attachments"][0].type == AttachmentType.VOICE

    def test_poll_m4a_voice_attachment(self, tmp_path: Path) -> None:
        """An .m4a audio attachment with voice MIME type should be detected."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        m4a_file = tmp_path / "voice.m4a"
        m4a_file.write_bytes(_make_m4a_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(
            db_path,
            rowid=1,
            filename=str(m4a_file),
            mime_type="audio/mp4",
            transfer_name="voice.m4a",
        )
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@icloud.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        att = messages[0]["attachments"][0]
        # audio/mp4 is generic audio, not a voice-specific MIME → AUDIO type
        assert att.type == AttachmentType.AUDIO
        assert att.file_path == m4a_file


# ---------------------------------------------------------------------------
# Tests: Missing voice attachment file handled gracefully
# ---------------------------------------------------------------------------


class TestMissingVoiceFile:
    """Test that missing voice attachment files don't crash the flow."""

    def test_missing_caf_file_still_returns_attachment(self, tmp_path: Path) -> None:
        """When the .caf file doesn't exist on disk, file_path is None."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(
            db_path,
            1,
            "/nonexistent/Library/Messages/Attachments/xx/yy/uuid/voice.caf",
            "audio/x-caf",
            "voice.caf",
            total_bytes=25000,
        )
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(allowed_senders=["friend@icloud.com"])
        channel.MESSAGES_DB = db_path

        messages = channel._poll(0)
        assert len(messages) == 1
        att = messages[0]["attachments"][0]
        assert att.type == AttachmentType.VOICE
        assert att.file_path is None  # file missing
        assert att.file_name == "voice.caf"

    def test_missing_file_chatserver_handles_gracefully(self, tmp_path: Path) -> None:
        """ChatServer should handle voice attachments with file_path=None
        without crashing — save will fail, fallback message is used."""
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        attachment = Attachment(
            type=AttachmentType.VOICE,
            file_path=None,  # file missing from disk
            mime_type="audio/x-caf",
            file_name="cleaned_up.caf",
        )

        mock_result = _make_agent_result("I couldn't process the audio.")

        with patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop:
            response = server.handle_message(
                "friend@icloud.com",
                "did you hear that?",
                attachments=[attachment],
            )

        assert response == "I couldn't process the audio."
        # Without a file on disk, transcription can't work — content should still be text
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        # Should contain a fallback message about the voice
        assert (
            "Voice message" in last_user["content"] or "did you hear that?" in last_user["content"]
        )


# ---------------------------------------------------------------------------
# Tests: listen() sends IncomingMessage to callback for voice messages
# ---------------------------------------------------------------------------


class TestListenVoiceCallback:
    """Verify listen() passes IncomingMessage to callback when voice attachments are present."""

    def test_voice_message_callback_gets_incoming_message(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(_make_caf_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(db_path, 1, str(caf_file), "audio/x-caf", "voice.caf")
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(
            allowed_senders=["friend@icloud.com"],
            poll_interval=1,
        )
        channel.MESSAGES_DB = db_path

        callback = MagicMock(return_value="heard you!")
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
        assert incoming.text is None
        assert incoming.channel == "imessage"
        assert len(incoming.attachments) == 1
        assert incoming.attachments[0].type == AttachmentType.VOICE
        assert incoming.attachments[0].file_path == caf_file

    def test_voice_response_sent_via_applescript(self, tmp_path: Path) -> None:
        """The agent's response should be sent back to the sender via send()."""
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(_make_caf_bytes())

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(db_path, 1, str(caf_file), "audio/x-caf", "voice.caf")
        _link_attachment(db_path, 1, 1)

        channel = IMessageChannel(
            allowed_senders=["friend@icloud.com"],
            poll_interval=1,
        )
        channel.MESSAGES_DB = db_path

        callback = MagicMock(return_value="I transcribed your voice!")

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

        mock_send.assert_called_once_with("friend@icloud.com", "I transcribed your voice!")


# ---------------------------------------------------------------------------
# Tests: CAF format conversion (ffmpeg)
# ---------------------------------------------------------------------------


class TestCafConversion:
    """Test that .caf files are converted before transcription."""

    def test_caf_is_in_needs_conversion_set(self) -> None:
        """Verify .caf is registered as needing conversion in TranscriptionService."""
        from creel.services.transcription import NEEDS_CONVERSION

        assert ".caf" in NEEDS_CONVERSION

    def test_caf_not_in_whisper_supported(self) -> None:
        """Verify .caf is NOT in Whisper's natively supported formats."""
        from creel.services.transcription import WHISPER_SUPPORTED_FORMATS

        assert ".caf" not in WHISPER_SUPPORTED_FORMATS

    def test_maybe_convert_calls_ffmpeg_for_caf(self, tmp_path: Path) -> None:
        """TranscriptionService._maybe_convert should call ffmpeg for .caf files."""
        from creel.services.transcription import TranscriptionService

        service = TranscriptionService(api_key="test-key")
        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(_make_caf_bytes())

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                # Create the expected output file so the method can find it
                wav_file = caf_file.with_suffix(".wav")
                wav_file.write_bytes(b"RIFF" + b"\x00" * 100)

                result = service._maybe_convert(caf_file)

        assert result == wav_file
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "ffmpeg"
        assert str(caf_file) in call_args
        assert str(wav_file) in call_args

    def test_maybe_convert_returns_original_without_ffmpeg(self, tmp_path: Path) -> None:
        """Without ffmpeg, _maybe_convert should return the original .caf path."""
        from creel.services.transcription import TranscriptionService

        service = TranscriptionService(api_key="test-key")
        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(_make_caf_bytes())

        with patch("shutil.which", return_value=None):
            result = service._maybe_convert(caf_file)

        assert result == caf_file  # fallback: send as-is


# ---------------------------------------------------------------------------
# Tests: Full E2E flow (iMessage voice → ChatServer → LLM → response)
# ---------------------------------------------------------------------------


class TestE2EIMessageVoice:
    """Full integration test: iMessage voice → ChatServer → transcription → LLM → response."""

    def test_full_voice_flow_via_chatserver(self, tmp_path: Path) -> None:
        """Simulate: .caf attachment on disk → ChatServer saves + transcribes → LLM gets text."""
        caf_bytes = _make_caf_bytes()
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        caf_file = tmp_path / "imessage_voice.caf"
        caf_file.write_bytes(caf_bytes)

        mock_result = _make_agent_result("You said you wanted pizza for dinner!")

        with (
            patch.object(
                server._transcription,
                "transcribe",
                return_value="I want pizza for dinner",
            ),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            attachment = Attachment(
                type=AttachmentType.VOICE,
                file_path=caf_file,
                mime_type="audio/x-caf",
                file_name="imessage_voice.caf",
            )
            response = server.handle_message(
                "friend@icloud.com",
                "",
                attachments=[attachment],
            )

        # Verify response
        assert response == "You said you wanted pizza for dinner!"

        # Verify LLM received transcribed text
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]

        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert len(user_msgs) >= 1
        last_user = user_msgs[-1]
        # Voice messages produce plain text (not content blocks)
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: I want pizza for dinner" in last_user["content"]

        # Verify media file was saved to disk in the media store
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.caf"))
        assert len(saved_files) >= 1
        assert saved_files[0].read_bytes() == caf_bytes

    def test_voice_with_accompanying_text(self, tmp_path: Path) -> None:
        """Voice message with text: both transcription and original text in the message."""
        caf_bytes = _make_caf_bytes()
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(caf_bytes)

        mock_result = _make_agent_result("Got your voice and text!")

        with (
            patch.object(
                server._transcription,
                "transcribe",
                return_value="Here is my voice note",
            ),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            attachment = Attachment(
                type=AttachmentType.VOICE,
                file_path=caf_file,
                mime_type="audio/x-caf",
                file_name="voice.caf",
            )
            response = server.handle_message(
                "friend@icloud.com",
                "please listen",
                attachments=[attachment],
            )

        assert response == "Got your voice and text!"

        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]

        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: Here is my voice note" in last_user["content"]
        assert "please listen" in last_user["content"]

    def test_transcription_failure_shows_fallback(self, tmp_path: Path) -> None:
        """When transcription fails, a fallback message is sent to the LLM."""
        caf_bytes = _make_caf_bytes()
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(caf_bytes)

        mock_result = _make_agent_result("I couldn't understand the audio.")

        with (
            patch.object(server._transcription, "transcribe", return_value=""),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            attachment = Attachment(
                type=AttachmentType.VOICE,
                file_path=caf_file,
                mime_type="audio/x-caf",
                file_name="voice.caf",
            )

            response = server.handle_message(
                "friend@icloud.com",
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
        saved_files = list(media_dir.rglob("*.caf"))
        assert len(saved_files) >= 1

    def test_full_flow_via_incoming_message(self, tmp_path: Path) -> None:
        """Simulate the DaemonService path: IncomingMessage → ChatServer."""
        caf_bytes = _make_caf_bytes()
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(caf_bytes)

        mock_result = _make_agent_result("Got your iMessage voice note!")

        incoming = IncomingMessage(
            sender_id="friend@icloud.com",
            text=None,
            attachments=[
                Attachment(
                    type=AttachmentType.VOICE,
                    file_path=caf_file,
                    mime_type="audio/x-caf",
                    file_name="voice.caf",
                )
            ],
            channel="imessage",
        )

        with (
            patch.object(
                server._transcription,
                "transcribe",
                return_value="This is from iMessage",
            ),
            patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            response = server.handle_message(
                incoming.sender_id,
                incoming.text or "",
                attachments=incoming.attachments,
            )

        assert response == "Got your iMessage voice note!"

        # Verify LLM received transcribed text
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: This is from iMessage" in last_user["content"]

        # Verify file on disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.caf"))
        assert len(saved_files) >= 1

    def test_full_flow_poll_to_chatserver(self, tmp_path: Path) -> None:
        """Full E2E: iMessage poll picks up voice → ChatServer transcribes → reply sent.

        This simulates the DaemonService wiring: channel.listen(callback)
        where callback routes IncomingMessage through ChatServer.handle_message.
        """
        caf_bytes = _make_caf_bytes()
        agent_def = _make_agent_def(tmp_path)

        # Set up chat.db with a voice message
        db_path = tmp_path / "chat.db"
        _create_chat_db(db_path)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(caf_bytes)

        _insert_handle(db_path, 1, "friend@icloud.com")
        _insert_message(db_path, 1, None, handle_id=1)
        _insert_attachment(db_path, 1, str(caf_file), "audio/x-caf", "voice.caf")
        _link_attachment(db_path, 1, 1)

        mock_result = _make_agent_result("I heard you say hello!")

        server = ChatServer(agent_def)

        channel = IMessageChannel(
            allowed_senders=["friend@icloud.com"],
            poll_interval=1,
        )
        channel.MESSAGES_DB = db_path

        sent_responses: list[tuple[str, str]] = []

        with (
            patch.object(
                server._transcription,
                "transcribe",
                return_value="Hello from iMessage voice",
            ),
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
        assert sent_responses[0] == ("friend@icloud.com", "I heard you say hello!")

        # Verify LLM received transcribed voice text
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert "[Voice message]: Hello from iMessage voice" in last_user["content"]

        # Verify media file saved to disk
        media_dir = tmp_path / "media"
        saved_files = list(media_dir.rglob("*.caf"))
        assert len(saved_files) >= 1
        assert saved_files[0].read_bytes() == caf_bytes

    def test_media_disabled_ignores_voice_attachments(self, tmp_path: Path) -> None:
        """When media is disabled, iMessage voice attachments are silently ignored."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)

        agent_def = AgentDefinition(
            system_prompt="You are a test assistant.",
            llm=LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100),
            agent=AgentConfig(max_turns=3),
            session=SessionConfig(
                sessions_dir=str(sessions_dir),
                summarize_on_trim=False,
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "nonexistent-workspace")),
            channels=ChannelsConfig(),
            media=None,  # media disabled
        )

        server = ChatServer(agent_def)

        caf_file = tmp_path / "voice.caf"
        caf_file.write_bytes(_make_caf_bytes())

        attachment = Attachment(
            type=AttachmentType.VOICE,
            file_path=caf_file,
            mime_type="audio/x-caf",
        )

        mock_result = _make_agent_result("Text only response")

        with patch("creel.chat.run_agent_loop", return_value=mock_result) as mock_loop:
            response = server.handle_message(
                "friend@icloud.com",
                "did you hear that?",
                attachments=[attachment],
            )

        assert response == "Text only response"
        # Content should be plain text (no voice transcription)
        call_kwargs = mock_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]
        assert isinstance(last_user["content"], str)
        assert "[Voice message]" not in last_user["content"]
