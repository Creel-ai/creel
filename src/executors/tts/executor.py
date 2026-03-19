#!/usr/bin/env python3
"""TTS executor — text-to-speech synthesis via multiple backends.

Supports ElevenLabs, OpenAI TTS, and local pyttsx3/espeak fallback.
Outputs JSON with path to the generated audio file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_BACKEND = "openai"


def register_skill():
    """Register the tts skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="tts",
        label="Text-to-Speech",
        tools=(
            ToolSpec(
                name="synthesize_speech",
                description="Convert text to speech audio using TTS",
                params=(
                    Param(
                        name="text",
                        type="string",
                        description="Text to convert to speech (max 5000 chars)",
                        required=True,
                    ),
                    Param(
                        name="voice",
                        type="string",
                        description="Voice name (e.g. nova, alloy, shimmer for OpenAI; Rachel for ElevenLabs)",
                    ),
                    Param(
                        name="backend",
                        type="string",
                        description="TTS backend: openai, elevenlabs, or local",
                    ),
                    Param(
                        name="output_format",
                        type="string",
                        description="Audio format: mp3, wav, ogg (default: mp3)",
                    ),
                    Param(
                        name="output_path",
                        type="string",
                        description="Custom output file path (optional)",
                    ),
                ),
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        text = config.args.get("text", "")
        if not text:
            raise ValueError("tts executor requires a 'text' argument")

        voice = config.args.get("voice") or None
        backend = config.args.get("backend") or None
        output_format = config.args.get("output_format") or None
        output_path = config.args.get("output_path") or None

        result = synthesize(
            text,
            voice=voice,
            backend=backend,
            output_format=output_format,
            output_path=output_path,
        )
        return json.dumps(result, indent=2)

    return meta, execute


DEFAULT_VOICE: dict[str, str] = {
    "elevenlabs": "Rachel",
    "openai": "nova",
    "local": "default",
}
DEFAULT_OUTPUT_FORMAT = "mp3"
DEFAULT_MAX_CHARS = 5000
DEFAULT_OUTPUT_DIR = os.environ.get("TTS_OUTPUT_DIR", "/tmp/creel_tts")

# Rate-limit: max requests per minute per backend
RATE_LIMIT_RPM = 20

# ElevenLabs
ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"

# OpenAI
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"

# Supported formats per backend
SUPPORTED_FORMATS: dict[str, set[str]] = {
    "elevenlabs": {"mp3", "wav", "ogg"},
    "openai": {"mp3", "wav", "ogg", "flac"},
    "local": {"wav"},
}

# ---------------------------------------------------------------------------
# Rate limiter (simple in-process token bucket)
# ---------------------------------------------------------------------------

_rate_state: dict[str, list[float]] = {}


def _check_rate_limit(backend: str) -> None:
    """Enforce per-backend rate limiting."""
    now = time.monotonic()
    window = _rate_state.setdefault(backend, [])
    # Purge entries older than 60 s
    _rate_state[backend] = [t for t in window if now - t < 60]
    if len(_rate_state[backend]) >= RATE_LIMIT_RPM:
        raise RuntimeError(
            f"Rate limit exceeded for {backend} backend ({RATE_LIMIT_RPM} requests/min)"
        )
    _rate_state[backend].append(now)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_key(text: str, voice: str, backend: str, fmt: str) -> str:
    """Deterministic cache key for a given synthesis request."""
    raw = f"{backend}:{voice}:{fmt}:{text}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _cache_path(key: str, fmt: str, output_dir: str) -> Path:
    return Path(output_dir) / f"{key}.{fmt}"


# ---------------------------------------------------------------------------
# Backend: ElevenLabs
# ---------------------------------------------------------------------------


def _resolve_elevenlabs_voice_id(voice: str, api_key: str) -> str:
    """Resolve a voice name to an ElevenLabs voice ID.

    If *voice* already looks like an ID (hex-ish, 20+ chars), return it as-is.
    """
    if len(voice) >= 20 and voice.isalnum():
        return voice

    resp = httpx.get(
        ELEVENLABS_VOICES_URL,
        headers={"xi-api-key": api_key},
        timeout=10.0,
    )
    resp.raise_for_status()
    for v in resp.json().get("voices", []):
        if v.get("name", "").lower() == voice.lower():
            return v["voice_id"]
    raise ValueError(f"ElevenLabs voice not found: {voice}")


def _synthesize_elevenlabs(
    text: str,
    voice: str,
    output_format: str,
    model: str,
    output_path: Path,
) -> Path:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    voice_id = _resolve_elevenlabs_voice_id(voice, api_key)

    format_map = {"mp3": "mp3_44100_128", "wav": "pcm_44100", "ogg": "ogg_vorbis"}
    el_format = format_map.get(output_format, "mp3_44100_128")

    resp = httpx.post(
        f"{ELEVENLABS_API_URL}/{voice_id}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        params={"output_format": el_format},
        timeout=60.0,
    )
    resp.raise_for_status()
    output_path.write_bytes(resp.content)
    return output_path


# ---------------------------------------------------------------------------
# Backend: OpenAI TTS
# ---------------------------------------------------------------------------


def _synthesize_openai(
    text: str,
    voice: str,
    output_format: str,
    output_path: Path,
) -> Path:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    valid_voices = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
    if voice.lower() not in valid_voices:
        voice = "nova"

    fmt = output_format if output_format in {"mp3", "wav", "ogg", "flac"} else "mp3"

    resp = httpx.post(
        OPENAI_TTS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "tts-1",
            "input": text,
            "voice": voice.lower(),
            "response_format": fmt,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    output_path.write_bytes(resp.content)
    return output_path


# ---------------------------------------------------------------------------
# Backend: Local (pyttsx3 / espeak)
# ---------------------------------------------------------------------------


def _synthesize_local(
    text: str,
    voice: str,
    output_path: Path,
) -> Path:
    try:
        import pyttsx3
    except ImportError as exc:
        raise RuntimeError("Local TTS requires pyttsx3: pip install pyttsx3") from exc

    engine = pyttsx3.init()

    if voice and voice != "default":
        for v in engine.getProperty("voices") or []:
            if voice.lower() in v.name.lower():
                engine.setProperty("voice", v.id)
                break

    wav_path = output_path.with_suffix(".wav")
    engine.save_to_file(text, str(wav_path))
    engine.runAndWait()
    return wav_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def synthesize(
    text: str,
    *,
    voice: str | None = None,
    backend: str | None = None,
    output_format: str | None = None,
    output_path: str | None = None,
    model: str | None = None,
    max_chars: int | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Synthesize speech from text.

    Returns a dict with ``audio_path``, ``backend``, ``voice``, ``format``,
    ``cached``, and ``chars`` keys.
    """
    backend = (backend or os.environ.get("TTS_BACKEND", DEFAULT_BACKEND)).lower()
    if backend not in ("elevenlabs", "openai", "local"):
        raise ValueError(f"Unsupported TTS backend: {backend}")

    voice = voice or os.environ.get("TTS_VOICE", DEFAULT_VOICE.get(backend, "default"))
    output_format = (
        output_format or os.environ.get("TTS_OUTPUT_FORMAT", DEFAULT_OUTPUT_FORMAT)
    ).lower()
    model = model or os.environ.get("TTS_MODEL", "eleven_turbo_v2")
    max_chars = max_chars or int(os.environ.get("TTS_MAX_CHARS", str(DEFAULT_MAX_CHARS)))
    out_dir = output_dir or DEFAULT_OUTPUT_DIR

    # Validate format for backend
    allowed = SUPPORTED_FORMATS.get(backend, set())
    if output_format not in allowed:
        output_format = next(iter(allowed))

    # Enforce character limit
    if len(text) > max_chars:
        raise ValueError(f"Text exceeds maximum length ({len(text)} > {max_chars} chars)")

    if not text.strip():
        raise ValueError("Text must not be empty")

    # Rate limit
    _check_rate_limit(backend)

    # Prepare output directory
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # Check cache
    key = _cache_key(text, voice, backend, output_format)
    if output_path:
        dest = Path(output_path)
    else:
        dest = _cache_path(key, output_format, out_dir)

    cached = False
    if dest.exists() and dest.stat().st_size > 0:
        cached = True
    else:
        if backend == "elevenlabs":
            dest = _synthesize_elevenlabs(text, voice, output_format, model, dest)
        elif backend == "openai":
            dest = _synthesize_openai(text, voice, output_format, dest)
        elif backend == "local":
            dest = _synthesize_local(text, voice, dest)

    return {
        "audio_path": str(dest),
        "backend": backend,
        "voice": voice,
        "format": output_format if backend != "local" else "wav",
        "cached": cached,
        "chars": len(text),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    text = os.environ.get("TEXT", "")
    if not text and len(sys.argv) > 1:
        text = sys.argv[1]

    if not text:
        print(json.dumps({"error": "TEXT is required"}), file=sys.stderr)
        sys.exit(1)

    backend = os.environ.get("BACKEND")
    voice = os.environ.get("VOICE")
    output_format = os.environ.get("OUTPUT_FORMAT")
    output_path = os.environ.get("OUTPUT_PATH")
    model = os.environ.get("MODEL")
    max_chars_str = os.environ.get("MAX_CHARS")
    max_chars = int(max_chars_str) if max_chars_str else None

    try:
        result = synthesize(
            text,
            voice=voice,
            backend=backend,
            output_format=output_format,
            output_path=output_path,
            model=model,
            max_chars=max_chars,
        )
        print(json.dumps(result, indent=2))
    except (httpx.HTTPError, ValueError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
