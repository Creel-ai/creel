"""Voice message transcription via OpenAI Whisper API or local whisper CLI."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Formats accepted by the OpenAI Whisper API
WHISPER_SUPPORTED_FORMATS = frozenset({
    ".flac", ".m4a", ".mp3", ".mp4", ".mpeg",
    ".mpga", ".oga", ".ogg", ".wav", ".webm",
})

# Formats that need conversion before sending to the API
NEEDS_CONVERSION = frozenset({".caf", ".opus", ".amr", ".aac"})

_API_URL = "https://api.openai.com/v1/audio/transcriptions"
_API_TIMEOUT = 60  # seconds


class TranscriptionService:
    """Transcribe audio files using OpenAI Whisper API or a local whisper CLI.

    Parameters
    ----------
    backend : str
        ``"openai"`` for the Whisper API, ``"local"`` for the CLI
        (falls back to API if the CLI is not installed).
    model : str
        Model identifier for the API (default ``"whisper-1"``).
    api_key : str | None
        Explicit API key.  When *None* the service reads
        ``OPENAI_API_KEY`` from the environment.
    """

    def __init__(
        self,
        backend: str = "openai",
        model: str = "whisper-1",
        api_key: str | None = None,
    ) -> None:
        self._backend = backend
        self._model = model
        self._api_key = api_key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def transcribe(self, file_path: Path) -> str:
        """Return the transcribed text for *file_path*, or ``""`` on failure."""
        file_path = Path(file_path)
        if not file_path.exists():
            logger.warning("Audio file not found: %s", file_path)
            return ""

        try:
            if self._backend == "local":
                return self._transcribe_local(file_path)
            return self._transcribe_openai(file_path)
        except Exception:
            logger.warning("Transcription failed for %s", file_path, exc_info=True)
            return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_api_key(self) -> str | None:
        """Return the API key from the explicit setting or environment."""
        return self._api_key or os.environ.get("OPENAI_API_KEY")

    def _maybe_convert(self, file_path: Path) -> Path:
        """Convert unsupported audio formats to wav using ffmpeg.

        Returns the original path when no conversion is needed or when
        ffmpeg is not available.
        """
        if file_path.suffix.lower() in WHISPER_SUPPORTED_FORMATS:
            return file_path

        if file_path.suffix.lower() not in NEEDS_CONVERSION:
            # Unknown format — try sending as-is
            return file_path

        if not shutil.which("ffmpeg"):
            logger.warning(
                "ffmpeg not found; sending %s to Whisper API as-is "
                "(transcription may fail)",
                file_path.suffix,
            )
            return file_path

        wav_path = file_path.with_suffix(".wav")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(file_path),
                    "-ar", "16000", "-ac", "1",
                    str(wav_path),
                ],
                capture_output=True,
                check=True,
                timeout=30,
            )
            logger.debug("Converted %s -> %s", file_path, wav_path)
            return wav_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("ffmpeg conversion failed for %s: %s", file_path, exc)
            return file_path

    @staticmethod
    def _cleanup_converted(original: Path, converted: Path) -> None:
        """Remove the converted temp file if it differs from the original."""
        if converted != original and converted.exists():
            try:
                converted.unlink()
                logger.debug("Cleaned up converted file: %s", converted)
            except OSError:
                logger.debug("Could not remove converted file: %s", converted)

    def _transcribe_openai(self, file_path: Path) -> str:
        """Call the OpenAI Whisper API."""
        api_key = self._resolve_api_key()
        if not api_key:
            logger.warning(
                "No OpenAI API key available for transcription "
                "(set OPENAI_API_KEY or pass api_key to TranscriptionService)"
            )
            return ""

        converted_path = self._maybe_convert(file_path)
        try:
            with open(converted_path, "rb") as f:
                response = httpx.post(
                    _API_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (converted_path.name, f, "application/octet-stream")},
                    data={"model": self._model},
                    timeout=_API_TIMEOUT,
                )

            response.raise_for_status()
            text = response.json().get("text", "").strip()
            logger.info(
                "Transcribed %s (%d chars)",
                converted_path.name,
                len(text),
            )
            return text
        finally:
            self._cleanup_converted(file_path, converted_path)

    def _transcribe_local(self, file_path: Path) -> str:
        """Shell out to the ``whisper`` CLI.  Falls back to the API."""
        if not shutil.which("whisper"):
            logger.info("Local whisper CLI not found, falling back to OpenAI API")
            return self._transcribe_openai(file_path)

        converted_path = self._maybe_convert(file_path)
        try:
            result = subprocess.run(
                [
                    "whisper",
                    str(converted_path),
                    "--model", self._model,
                    "--output_format", "txt",
                    "--output_dir", str(converted_path.parent),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                logger.warning("Local whisper failed: %s", result.stderr)
                return self._transcribe_openai(file_path)

            txt_path = converted_path.with_suffix(".txt")
            if txt_path.exists():
                text = txt_path.read_text().strip()
                txt_path.unlink(missing_ok=True)
                logger.info("Transcribed (local) %s (%d chars)", converted_path.name, len(text))
                return text

            logger.warning("Whisper produced no output for %s", converted_path)
            return ""
        finally:
            self._cleanup_converted(file_path, converted_path)
