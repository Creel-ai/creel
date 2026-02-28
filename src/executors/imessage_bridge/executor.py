#!/usr/bin/env python3
"""iMessage Bridge executor - bridge-calling executor for iMessage via imsg CLI.

Instead of running the imsg CLI directly, this executor makes HTTP calls
to the bridge server which executes imsg CLI commands on the host.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def call_bridge(
    endpoint: str, data: dict[str, Any] | None = None, timeout: int = 30
) -> dict:
    """Make an HTTP call to the bridge server.

    Args:
        endpoint: Bridge endpoint path (e.g., '/imessage/recent')
        data: Request body data (optional)
        timeout: Request timeout in seconds

    Returns:
        Bridge response as dict

    Raises:
        RuntimeError: If bridge call fails or returns error
    """
    bridge_url = os.environ.get("BRIDGE_URL")
    bridge_token = os.environ.get("BRIDGE_TOKEN")

    if not bridge_url:
        raise RuntimeError("BRIDGE_URL environment variable not set")
    if not bridge_token:
        raise RuntimeError("BRIDGE_TOKEN environment variable not set")

    url = f"{bridge_url}{endpoint}"
    headers = {
        "Authorization": f"Bearer {bridge_token}",
        "Content-Type": "application/json",
    }

    try:
        if data is not None:
            response = requests.post(url, json=data, headers=headers, timeout=timeout)
        else:
            response = requests.post(url, json={}, headers=headers, timeout=timeout)

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Bridge error: {result.get('error', 'Unknown error')}")

        return result

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Bridge request failed: {e}") from e


def get_recent(limit: int = 20) -> dict[str, Any]:
    """Get recent iMessages via bridge."""
    return call_bridge("/imessage/recent", {"limit": limit})


def send_message(to: str, text: str) -> dict[str, Any]:
    """Send iMessage via bridge."""
    return call_bridge("/imessage/send", {"to": to, "text": text})


def get_chats() -> dict[str, Any]:
    """Get iMessage chats via bridge."""
    return call_bridge("/imessage/chats")


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "recent")

    try:
        if action == "recent":
            limit = int(os.environ.get("LIMIT", "20"))
            result = get_recent(limit)

        elif action == "send":
            to = os.environ.get("TO")
            text = os.environ.get("TEXT")

            if not to:
                raise ValueError("TO environment variable required for send action")
            if not text:
                raise ValueError("TEXT environment variable required for send action")

            result = send_message(to, text)

        elif action == "chats":
            result = get_chats()

        else:
            raise ValueError(f"Unknown action: {action}")

        # Output the bridge response output
        print(result.get("output", ""))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
