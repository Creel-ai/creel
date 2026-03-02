"""Validation helpers for API keys and bot tokens.

Each validator makes a lightweight HTTP call and returns a ``ValidationResult``.
Network errors produce ``ok=False`` with a descriptive message — they never raise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

_TIMEOUT = 15.0


@dataclass
class ValidationResult:
    """Outcome of a credential validation check."""

    ok: bool
    message: str
    detail: dict[str, Any] | None = field(default=None, repr=False)


def validate_anthropic_key(api_key: str) -> ValidationResult:
    """Validate an Anthropic API key via a read-only ``GET /v1/models`` call."""
    try:
        resp = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return ValidationResult(ok=False, message=f"Network error: {exc}")

    if resp.status_code == 200:
        return ValidationResult(ok=True, message="API key is valid")
    if resp.status_code == 401:
        return ValidationResult(ok=False, message="Invalid API key (401 Unauthorized)")
    # 429 / 5xx — key format is accepted, just rate-limited or server issue
    if resp.status_code in (429, 500, 502, 503, 529):
        return ValidationResult(
            ok=True,
            message=f"Key accepted (server returned {resp.status_code}, likely rate-limited)",
        )
    return ValidationResult(
        ok=False,
        message=f"Unexpected response ({resp.status_code})",
        detail={"status_code": resp.status_code},
    )


def validate_openai_key(api_key: str) -> ValidationResult:
    """Validate an OpenAI API key via ``GET /v1/models``."""
    try:
        resp = httpx.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return ValidationResult(ok=False, message=f"Network error: {exc}")

    if resp.status_code == 200:
        return ValidationResult(ok=True, message="API key is valid")
    if resp.status_code == 401:
        return ValidationResult(ok=False, message="Invalid API key (401 Unauthorized)")
    if resp.status_code in (429, 500, 502, 503):
        return ValidationResult(
            ok=True,
            message=f"Key accepted (server returned {resp.status_code}, likely rate-limited)",
        )
    return ValidationResult(
        ok=False,
        message=f"Unexpected response ({resp.status_code})",
        detail={"status_code": resp.status_code},
    )


def validate_ollama_reachable(base_url: str) -> ValidationResult:
    """Check that an Ollama instance is reachable via ``GET /api/tags``."""
    url = base_url.rstrip("/") + "/api/tags"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        return ValidationResult(ok=False, message=f"Cannot reach Ollama at {base_url}: {exc}")

    if resp.status_code == 200:
        try:
            data = resp.json()
            models = [m.get("name", "?") for m in data.get("models", [])]
        except Exception:
            models = []
        return ValidationResult(
            ok=True,
            message=f"Ollama reachable ({len(models)} model(s) available)",
            detail={"models": models},
        )
    return ValidationResult(
        ok=False,
        message=f"Ollama returned {resp.status_code}",
        detail={"status_code": resp.status_code},
    )


def validate_telegram_token(bot_token: str) -> ValidationResult:
    """Validate a Telegram bot token via ``getMe``."""
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return ValidationResult(ok=False, message=f"Network error: {exc}")

    if resp.status_code == 200:
        try:
            data = resp.json()
            username = data.get("result", {}).get("username", "unknown")
        except Exception:
            username = "unknown"
        return ValidationResult(
            ok=True,
            message=f"Bot token valid (bot: @{username})",
            detail={"username": username},
        )
    if resp.status_code == 401:
        return ValidationResult(ok=False, message="Invalid bot token (401 Unauthorized)")
    return ValidationResult(
        ok=False,
        message=f"Unexpected response ({resp.status_code})",
        detail={"status_code": resp.status_code},
    )
