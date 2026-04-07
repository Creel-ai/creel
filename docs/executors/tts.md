# Text-to-Speech (TTS)

The TTS executor synthesizes speech from text using multiple backends.

## Backends

| Backend | API Key Required | Default Voice | Formats |
|---------|-----------------|---------------|---------|
| **OpenAI** | `OPENAI_API_KEY` | nova | mp3, wav, ogg |
| **ElevenLabs** | `ELEVENLABS_API_KEY` | Rachel | mp3, wav, ogg |
| **Local** (pyttsx3) | None | System default | wav |

## Tools

### `synthesize_speech`

Convert text to an audio file.

```json
{
  "tool": "synthesize_speech",
  "args": {
    "text": "Good morning. Here is your briefing.",
    "backend": "openai",
    "voice": "nova",
    "output_format": "mp3"
  }
}
```

**Parameters:**

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `text` | Yes | — | Text to synthesize (max 5000 chars) |
| `backend` | No | `openai` | `openai`, `elevenlabs`, or `local` |
| `voice` | No | Backend default | Voice name |
| `output_format` | No | `mp3` | `mp3`, `wav`, or `ogg` |
| `output_path` | No | Auto-generated | Custom output file path |

**Returns:** JSON with `audio_path`, `backend`, `voice`, `format`, `cached`, and `chars`.

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_BACKEND` | `openai` | Default backend |
| `TTS_VOICE` | Backend default | Default voice |
| `TTS_OUTPUT_FORMAT` | `mp3` | Default output format |
| `TTS_OUTPUT_DIR` | `/tmp/creel_tts` | Cache directory |
| `TTS_MAX_CHARS` | `5000` | Max text length |
| `TTS_MODEL` | `eleven_turbo_v2` | ElevenLabs model ID |

## Features

- **Caching**: Audio files are cached using a SHA256 key derived from text, voice, backend, and format. Repeated requests return the cached file.
- **Rate limiting**: Max 20 requests per minute per backend.
