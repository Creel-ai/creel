"""Tests for the TTS executor."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from executors.tts.executor import (
    _cache_key,
    _check_rate_limit,
    _rate_state,
    synthesize,
)

# ---------------------------------------------------------------------------
# Cache key tests
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_deterministic(self) -> None:
        k1 = _cache_key("hello", "nova", "openai", "mp3")
        k2 = _cache_key("hello", "nova", "openai", "mp3")
        assert k1 == k2

    def test_different_text(self) -> None:
        k1 = _cache_key("hello", "nova", "openai", "mp3")
        k2 = _cache_key("world", "nova", "openai", "mp3")
        assert k1 != k2

    def test_different_voice(self) -> None:
        k1 = _cache_key("hello", "nova", "openai", "mp3")
        k2 = _cache_key("hello", "alloy", "openai", "mp3")
        assert k1 != k2

    def test_different_backend(self) -> None:
        k1 = _cache_key("hello", "nova", "openai", "mp3")
        k2 = _cache_key("hello", "nova", "elevenlabs", "mp3")
        assert k1 != k2

    def test_key_length(self) -> None:
        k = _cache_key("hello", "nova", "openai", "mp3")
        assert len(k) == 24


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------


class TestRateLimit:
    def setup_method(self) -> None:
        _rate_state.clear()

    def test_allows_first_request(self) -> None:
        _check_rate_limit("openai")  # should not raise

    def test_blocks_after_limit(self) -> None:

        for _ in range(20):
            _check_rate_limit("test_backend")
        with pytest.raises(RuntimeError, match="Rate limit exceeded"):
            _check_rate_limit("test_backend")

    def test_separate_backends(self) -> None:
        for _ in range(20):
            _check_rate_limit("backend_a")
        # Different backend should still work
        _check_rate_limit("backend_b")


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestSynthesizeValidation:
    def test_empty_text_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            synthesize("", backend="openai", output_dir=str(tmp_path))

    def test_whitespace_only_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            synthesize("   ", backend="openai", output_dir=str(tmp_path))

    def test_exceeds_max_chars(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="exceeds maximum length"):
            synthesize(
                "x" * 100,
                backend="openai",
                max_chars=50,
                output_dir=str(tmp_path),
            )

    def test_unsupported_backend(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unsupported TTS backend"):
            synthesize("hello", backend="invalid", output_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# OpenAI backend tests
# ---------------------------------------------------------------------------


class TestOpenAIBackend:
    def setup_method(self) -> None:
        _rate_state.clear()

    @patch("executors.tts.executor.httpx.post")
    def test_synthesize_openai_success(self, mock_post: MagicMock, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.content = b"fake-audio-bytes"
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = synthesize(
                "Hello world",
                backend="openai",
                voice="nova",
                output_format="mp3",
                output_dir=str(tmp_path),
            )

        assert result["backend"] == "openai"
        assert result["voice"] == "nova"
        assert result["format"] == "mp3"
        assert result["cached"] is False
        assert result["chars"] == 11
        assert Path(result["audio_path"]).exists()
        assert Path(result["audio_path"]).read_bytes() == b"fake-audio-bytes"

        # Verify the API call
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["json"]["model"] == "tts-1"
        assert call_kwargs.kwargs["json"]["voice"] == "nova"
        assert call_kwargs.kwargs["json"]["input"] == "Hello world"

    @patch("executors.tts.executor.httpx.post")
    def test_openai_invalid_voice_sent_as_nova(self, mock_post: MagicMock, tmp_path: Path) -> None:
        """Invalid voice names are coerced to 'nova' in the API call."""
        mock_resp = MagicMock()
        mock_resp.content = b"audio"
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            synthesize(
                "test",
                backend="openai",
                voice="nonexistent",
                output_dir=str(tmp_path),
            )

        # The API call should use "nova" as the fallback voice
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["json"]["voice"] == "nova"

    def test_openai_missing_key(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
                synthesize(
                    "hello",
                    backend="openai",
                    output_dir=str(tmp_path),
                )


# ---------------------------------------------------------------------------
# ElevenLabs backend tests
# ---------------------------------------------------------------------------


class TestElevenLabsBackend:
    def setup_method(self) -> None:
        _rate_state.clear()

    @patch("executors.tts.executor.httpx.post")
    @patch("executors.tts.executor.httpx.get")
    def test_synthesize_elevenlabs_success(
        self, mock_get: MagicMock, mock_post: MagicMock, tmp_path: Path
    ) -> None:
        # Mock voice listing
        mock_voices_resp = MagicMock()
        mock_voices_resp.json.return_value = {
            "voices": [
                {"name": "Rachel", "voice_id": "abc123def456"},
            ]
        }
        mock_voices_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_voices_resp

        # Mock synthesis
        mock_synth_resp = MagicMock()
        mock_synth_resp.content = b"elevenlabs-audio"
        mock_synth_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_synth_resp

        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "el-test-key"}):
            result = synthesize(
                "Hello from ElevenLabs",
                backend="elevenlabs",
                voice="Rachel",
                output_format="mp3",
                output_dir=str(tmp_path),
            )

        assert result["backend"] == "elevenlabs"
        assert result["voice"] == "Rachel"
        assert result["format"] == "mp3"
        assert result["cached"] is False
        assert Path(result["audio_path"]).read_bytes() == b"elevenlabs-audio"

    @patch("executors.tts.executor.httpx.get")
    def test_elevenlabs_voice_not_found(self, mock_get: MagicMock, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"voices": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "el-test-key"}):
            with pytest.raises(ValueError, match="voice not found"):
                synthesize(
                    "hello",
                    backend="elevenlabs",
                    voice="Nonexistent",
                    output_dir=str(tmp_path),
                )

    def test_elevenlabs_missing_key(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY is not set"):
                synthesize(
                    "hello",
                    backend="elevenlabs",
                    output_dir=str(tmp_path),
                )


# ---------------------------------------------------------------------------
# Local backend tests
# ---------------------------------------------------------------------------


class TestLocalBackend:
    def setup_method(self) -> None:
        _rate_state.clear()

    @patch("executors.tts.executor.pyttsx3", create=True)
    def test_synthesize_local_success(self, mock_pyttsx3_mod: MagicMock, tmp_path: Path) -> None:
        mock_engine = MagicMock()
        mock_pyttsx3_mod.init.return_value = mock_engine
        mock_engine.getProperty.return_value = []

        # Make save_to_file write a dummy wav
        def fake_save(text: str, path: str) -> None:
            Path(path).write_bytes(b"RIFF-fake-wav")

        mock_engine.save_to_file.side_effect = fake_save

        with patch.dict("sys.modules", {"pyttsx3": mock_pyttsx3_mod}):
            result = synthesize(
                "Local TTS test",
                backend="local",
                output_dir=str(tmp_path),
            )

        assert result["backend"] == "local"
        assert result["format"] == "wav"
        assert Path(result["audio_path"]).exists()

    def test_local_format_forced_to_wav(self, tmp_path: Path) -> None:
        """Local backend only supports wav, format should be coerced."""
        mock_pyttsx3 = MagicMock()
        mock_engine = MagicMock()
        mock_pyttsx3.init.return_value = mock_engine
        mock_engine.getProperty.return_value = []

        def fake_save(text: str, path: str) -> None:
            Path(path).write_bytes(b"wav-data")

        mock_engine.save_to_file.side_effect = fake_save

        with patch.dict("sys.modules", {"pyttsx3": mock_pyttsx3}):
            result = synthesize(
                "test",
                backend="local",
                output_format="mp3",  # request mp3, should get wav
                output_dir=str(tmp_path),
            )

        assert result["format"] == "wav"


# ---------------------------------------------------------------------------
# Caching tests
# ---------------------------------------------------------------------------


class TestCaching:
    def setup_method(self) -> None:
        _rate_state.clear()

    @patch("executors.tts.executor.httpx.post")
    def test_returns_cached_on_second_call(self, mock_post: MagicMock, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.content = b"audio-data"
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            r1 = synthesize(
                "cache me",
                backend="openai",
                voice="nova",
                output_dir=str(tmp_path),
            )
            assert r1["cached"] is False

            r2 = synthesize(
                "cache me",
                backend="openai",
                voice="nova",
                output_dir=str(tmp_path),
            )
            assert r2["cached"] is True

        # API should have been called only once
        assert mock_post.call_count == 1

    @patch("executors.tts.executor.httpx.post")
    def test_custom_output_path(self, mock_post: MagicMock, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.content = b"custom-audio"
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        custom = str(tmp_path / "my_audio.mp3")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = synthesize(
                "custom path test",
                backend="openai",
                output_path=custom,
                output_dir=str(tmp_path),
            )

        assert result["audio_path"] == custom
        assert Path(custom).read_bytes() == b"custom-audio"


# ---------------------------------------------------------------------------
# CLI / main() tests
# ---------------------------------------------------------------------------


class TestMain:
    @patch("executors.tts.executor.synthesize")
    def test_main_success(self, mock_synth: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        mock_synth.return_value = {
            "audio_path": "/tmp/test.mp3",
            "backend": "openai",
            "voice": "nova",
            "format": "mp3",
            "cached": False,
            "chars": 5,
        }

        with patch.dict("os.environ", {"TEXT": "hello"}):
            from executors.tts.executor import main

            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["audio_path"] == "/tmp/test.mp3"

    def test_main_no_text_exits(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with patch("sys.argv", ["executor.py"]):
                from executors.tts.executor import main

                with pytest.raises(SystemExit, match="1"):
                    main()


# ---------------------------------------------------------------------------
# Format validation
# ---------------------------------------------------------------------------


class TestFormatValidation:
    def setup_method(self) -> None:
        _rate_state.clear()

    @patch("executors.tts.executor.httpx.post")
    def test_unsupported_format_falls_back(self, mock_post: MagicMock, tmp_path: Path) -> None:
        mock_resp = MagicMock()
        mock_resp.content = b"audio"
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = synthesize(
                "test",
                backend="openai",
                output_format="aac",  # not supported by openai
                output_dir=str(tmp_path),
            )

        # Should fall back to first supported format
        assert result["format"] in {"mp3", "wav", "ogg", "flac"}
