#!/usr/bin/env python3
"""Apple Contacts executor - bridge-calling executor for macOS Contacts app.

Instead of running AppleScript directly, this executor makes HTTP calls
to the bridge server which executes osascript commands on the host.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def register_skill():
    """Register the apple_contacts skill with the skill registry."""
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="apple_contacts",
        label="Apple Contacts",
        tools=(
            ToolSpec(
                name="search_contacts",
                description="Search contacts in the macOS Contacts app by name",
                params=(
                    Param(
                        name="query",
                        type="string",
                        description="Name to search for",
                        required=True,
                    ),
                ),
                fixed_args={"action": "search"},
            ),
            ToolSpec(
                name="get_contact",
                description=(
                    "Get full details for a contact (emails, phones, organization, title, note)"
                ),
                params=(
                    Param(
                        name="name",
                        type="string",
                        description="Name of the contact to look up",
                        required=True,
                    ),
                ),
                fixed_args={"action": "get"},
            ),
        ),
        needs_bridge=True,
        bridge_scope="CONTACTS",
        platform="darwin",
    )

    def execute(config: ExecutorConfig) -> str:
        action = config.args.get("action", "search")

        if action == "search":
            query = config.args.get("query", "")
            if not query:
                return json.dumps({"error": "query is required"})
            result = search_contacts(query)
        elif action == "get":
            name = config.args.get("name", "")
            if not name:
                return json.dumps({"error": "name is required"})
            result = get_contact(name)
        else:
            return json.dumps({"error": f"Unknown action: {action}"})

        return json.dumps(result, indent=2)

    return meta, execute


def call_bridge(endpoint: str, data: dict[str, Any] | None = None, timeout: int = 30) -> dict:
    """Make an HTTP call to the bridge server.

    Args:
        endpoint: Bridge endpoint path (e.g., '/contacts/search')
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


def search_contacts(query: str) -> dict[str, Any]:
    """Search contacts via bridge."""
    return call_bridge("/contacts/search", {"query": query})


def get_contact(name: str) -> dict[str, Any]:
    """Get contact details via bridge."""
    return call_bridge("/contacts/get", {"name": name})


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "search")

    try:
        if action == "search":
            query = os.environ.get("QUERY")
            if not query:
                raise ValueError("QUERY environment variable required for search action")
            result = search_contacts(query)

        elif action == "get":
            name = os.environ.get("NAME")
            if not name:
                raise ValueError("NAME environment variable required for get action")
            result = get_contact(name)

        else:
            raise ValueError(f"Unknown action: {action}")

        # Output the bridge response output
        print(result.get("output", ""))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
