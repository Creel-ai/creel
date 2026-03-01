"""Tests for TranscriptionService (MEDIA-003)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from creel.services.transcription import (
    NEEDS_CONVERSION,
    WHISPER_SUPPORTED_FORMATS,
    TranscriptionService,
)


class TestTranscribeOpenAI:
    """Transcription via the OpenAI Whisper API."""

    def test_transcribe_returns_text(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"OggS" + b"\x00" * 50)

        svc = TranscriptionService(api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "Hello, this is a test"}
        fake_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=fake_resp) as mock_post:
            result = svc.transcribe(audio)

        assert result == "Hello, this is a test"
        # Verify the API was called with correct params
        call_kwargs = mock_post.call_args
        assert "Bearer sk-test" in str(call_kwargs)
        assert call_kwargs.kwargs["data"] == {"model": "whisper-1"}

    def test_transcribe_strips_whitespace(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "  hello world  \n"}
        fake_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=fake_resp):
            assert svc.transcribe(audio) == "hello world"

    def test_api_key_from_env(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService()  # no explicit key
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "hello"}
        fake_resp.raise_for_status = MagicMock()

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env"}),
            patch("httpx.post", return_value=fake_resp) as mock_post,
        ):
            result = svc.transcribe(audio)

        assert result == "hello"
        assert "Bearer sk-env" in str(mock_post.call_args)

    def test_no_api_key_returns_empty(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService()  # no key
        with patch.dict("os.environ", {}, clear=True):
            result = svc.transcribe(audio)

        assert result == ""

    def test_api_error_returns_empty(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(api_key="sk-test")
        import httpx

        with patch(
            "httpx.post",
            side_effect=httpx.HTTPStatusError(
                "Server error", request=MagicMock(), response=MagicMock()
            ),
        ):
            result = svc.transcribe(audio)

        assert result == ""

    def test_custom_model(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(api_key="sk-test", model="whisper-large-v3")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "ok"}
        fake_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=fake_resp) as mock_post:
            svc.transcribe(audio)

        assert mock_post.call_args.kwargs["data"] == {"model": "whisper-large-v3"}


class TestTranscribeLocal:
    """Transcription via the local whisper CLI."""

    def test_local_whisper_success(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(backend="local", api_key="sk-test")

        def fake_run(cmd, **kwargs):
            # Simulate whisper writing a .txt file
            txt_path = audio.with_suffix(".txt")
            txt_path.write_text("transcribed locally")
            return MagicMock(returncode=0)

        with (
            patch("shutil.which", return_value="/usr/local/bin/whisper"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = svc.transcribe(audio)

        assert result == "transcribed locally"
        # whisper output file should be cleaned up
        assert not audio.with_suffix(".txt").exists()

    def test_local_fallback_to_api_when_cli_missing(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(backend="local", api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "api fallback"}
        fake_resp.raise_for_status = MagicMock()

        with (
            patch("shutil.which", return_value=None),  # no whisper CLI
            patch("httpx.post", return_value=fake_resp),
        ):
            result = svc.transcribe(audio)

        assert result == "api fallback"

    def test_local_fallback_to_api_on_cli_error(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(backend="local", api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "api after cli fail"}
        fake_resp.raise_for_status = MagicMock()

        with (
            patch("shutil.which", return_value="/usr/local/bin/whisper"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stderr="whisper error"),
            ),
            patch("httpx.post", return_value=fake_resp),
        ):
            result = svc.transcribe(audio)

        assert result == "api after cli fail"


class TestFileNotFound:
    """Transcribing a missing file returns empty string."""

    def test_missing_file(self):
        svc = TranscriptionService(api_key="sk-test")
        result = svc.transcribe(Path("/nonexistent/voice.ogg"))
        assert result == ""


class TestAudioConversion:
    """Unsupported formats are converted via ffmpeg before transcription."""

    def test_caf_converted_to_wav(self, tmp_path: Path):
        audio = tmp_path / "voice.caf"
        audio.write_bytes(b"caf-data")

        svc = TranscriptionService(api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "converted"}
        fake_resp.raise_for_status = MagicMock()

        wav_path = audio.with_suffix(".wav")

        def fake_run(cmd, **kwargs):
            # Simulate ffmpeg writing the wav file
            wav_path.write_bytes(b"wav-data")
            return MagicMock(returncode=0)

        with (
            patch("shutil.which", return_value="/usr/local/bin/ffmpeg"),
            patch("subprocess.run", side_effect=fake_run) as mock_run,
            patch("httpx.post", return_value=fake_resp) as mock_post,
        ):
            result = svc.transcribe(audio)

        assert result == "converted"
        # Verify ffmpeg was called
        ffmpeg_cmd = mock_run.call_args[0][0]
        assert ffmpeg_cmd[0] == "ffmpeg"
        assert str(audio) in ffmpeg_cmd
        # Verify the wav file was sent to the API (not the caf)
        files_arg = mock_post.call_args.kwargs["files"]
        assert files_arg["file"][0] == "voice.wav"

    def test_caf_no_ffmpeg_sends_original(self, tmp_path: Path):
        audio = tmp_path / "voice.caf"
        audio.write_bytes(b"caf-data")

        svc = TranscriptionService(api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "raw caf"}
        fake_resp.raise_for_status = MagicMock()

        with (
            patch("shutil.which", return_value=None),  # no ffmpeg
            patch("httpx.post", return_value=fake_resp) as mock_post,
        ):
            result = svc.transcribe(audio)

        assert result == "raw caf"
        # Original .caf file sent (not converted)
        files_arg = mock_post.call_args.kwargs["files"]
        assert files_arg["file"][0] == "voice.caf"

    def test_ffmpeg_failure_sends_original(self, tmp_path: Path):
        audio = tmp_path / "voice.caf"
        audio.write_bytes(b"caf-data")

        svc = TranscriptionService(api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "after ffmpeg fail"}
        fake_resp.raise_for_status = MagicMock()

        import subprocess

        with (
            patch("shutil.which", return_value="/usr/local/bin/ffmpeg"),
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")),
            patch("httpx.post", return_value=fake_resp) as mock_post,
        ):
            result = svc.transcribe(audio)

        assert result == "after ffmpeg fail"
        files_arg = mock_post.call_args.kwargs["files"]
        assert files_arg["file"][0] == "voice.caf"

    def test_ogg_not_converted(self, tmp_path: Path):
        """Supported formats skip conversion entirely."""
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "ogg direct"}
        fake_resp.raise_for_status = MagicMock()

        with (
            patch("subprocess.run") as mock_run,
            patch("httpx.post", return_value=fake_resp),
        ):
            result = svc.transcribe(audio)

        assert result == "ogg direct"
        mock_run.assert_not_called()


class TestSupportedFormats:
    """Verify the supported format sets are correct."""

    def test_whisper_formats_include_telegram_voice(self):
        # Telegram sends .oga/.ogg for voice
        assert ".oga" in WHISPER_SUPPORTED_FORMATS
        assert ".ogg" in WHISPER_SUPPORTED_FORMATS

    def test_caf_needs_conversion(self):
        # iMessage voice is .caf
        assert ".caf" in NEEDS_CONVERSION

    def test_common_formats_supported(self):
        for ext in (".mp3", ".wav", ".m4a", ".flac"):
            assert ext in WHISPER_SUPPORTED_FORMATS


class TestBackendSelection:
    """Backend param selects the transcription method."""

    def test_default_backend_is_openai(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(api_key="sk-test")
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"text": "openai"}
        fake_resp.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=fake_resp):
            result = svc.transcribe(audio)

        assert result == "openai"

    def test_local_backend_uses_cli(self, tmp_path: Path):
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"audio")

        svc = TranscriptionService(backend="local", api_key="sk-test")

        def fake_run(cmd, **kwargs):
            audio.with_suffix(".txt").write_text("local result")
            return MagicMock(returncode=0)

        with (
            patch("shutil.which", return_value="/usr/local/bin/whisper"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = svc.transcribe(audio)

        assert result == "local result"
