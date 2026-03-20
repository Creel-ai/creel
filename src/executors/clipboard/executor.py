#!/usr/bin/env python3
"""Clipboard executor - bridge-calling executor for macOS clipboard.

Instead of running pbcopy/pbpaste directly, this executor makes HTTP calls
to the bridge server which executes clipboard commands on the host.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def register_skill():
    """Register the clipboard skill with the skill registry."""
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="clipboard",
        label="Clipboard",
        tools=(
            ToolSpec(
                name="read_clipboard",
                description="Read current clipboard contents",
                params=(),
                fixed_args={"action": "read"},
            ),
            ToolSpec(
                name="write_clipboard",
                description="Write text to clipboard",
                params=(
                    Param(
                        name="text",
                        type="string",
                        description="Text to copy to clipboard",
                        required=True,
                    ),
                ),
                fixed_args={"action": "write"},
            ),
        ),
        needs_bridge=True,
        bridge_scope="CLIPBOARD",
        platform="darwin",
    )

    def execute(config: ExecutorConfig) -> str:
        action = config.args.get("action", "read")

        if action == "read":
            result = read_clipboard()
        elif action == "write":
            text = config.args.get("text", "")
            result = write_clipboard(text)
        else:
            raise ValueError(f"Unknown clipboard action: {action}")

        return json.dumps(result, indent=2)

    return meta, execute


def call_bridge(endpoint: str, data: dict[str, Any] | None = None, timeout: int = 30) -> dict:
    """Make an HTTP call to the bridge server.

    Args:
        endpoint: Bridge endpoint path (e.g., '/clipboard/read')
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


def read_clipboard() -> dict[str, Any]:
    """Read clipboard content via bridge."""
    return call_bridge("/clipboard/read")


def write_clipboard(text: str) -> dict[str, Any]:
    """Write text to clipboard via bridge."""
    return call_bridge("/clipboard/write", {"text": text})


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "read")

    try:
        if action == "read":
            result = read_clipboard()
        elif action == "write":
            text = os.environ.get("TEXT")
            if not text:
                raise ValueError("TEXT environment variable required for write action")
            result = write_clipboard(text)
        else:
            raise ValueError(f"Unknown action: {action}")

        # Output the bridge response output
        print(result.get("output", ""))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
