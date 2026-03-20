"""Output routing - sends LLM results to configured destinations."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from creel.models import OutputConfig

MESSAGE_PREFIX = "[safe-claw]"


def send_output(text: str, config: OutputConfig) -> None:
    """Route output to the configured destination.

    Args:
        text: The LLM response text to send.
        config: Output configuration (type and destination).
    """
    text = f"{MESSAGE_PREFIX} {text}"

    handlers = {
        "imessage": _send_imessage,
        "stdout": _send_stdout,
        "file": _send_file,
        "telegram": _send_telegram,
    }

    handler = handlers.get(config.type)
    if handler is None:
        raise ValueError(f"Unknown output type: {config.type}")

    handler(text, config.to)


def _send_imessage(text: str, to: str) -> None:
    """Send a message via iMessage using AppleScript (macOS only)."""
    if sys.platform != "darwin":
        raise RuntimeError("iMessage output is only available on macOS")

    # Validate phone number format (basic check)
    cleaned = (
        to.replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    )
    if not cleaned.isdigit():
        raise ValueError(f"Invalid phone number: {to}")

    # Escape the text for AppleScript
    escaped_text = text.replace("\\", "\\\\").replace('"', '\\"')

    applescript = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{to}" of targetService
        send "{escaped_text}" to targetBuddy
    end tell
    '''

    subprocess.run(
        ["osascript", "-e", applescript],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def _send_stdout(text: str, to: str) -> None:
    """Print output to stdout."""
    print(text)


def _send_file(text: str, to: str) -> None:
    """Write output to a file."""
    path = Path(to)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _send_telegram(text: str, to: str) -> None:
    """Send a message via the Telegram Bot API.

    Requires TELEGRAM_BOT_TOKEN in the environment.
    ``to`` is the Telegram chat ID.
    """
    import httpx

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable not set")

    if not to:
        raise ValueError("Telegram chat ID must be provided in output.to")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": to, "text": text, "parse_mode": "Markdown"}
    response = httpx.post(url, json=payload, timeout=30)
    response.raise_for_status()
