#!/usr/bin/env python3
"""Things 3 executor - bridge-calling executor for Things 3 task manager.

Instead of running the Things CLI directly, this executor makes HTTP calls
to the bridge server which executes things CLI commands on the host.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def call_bridge(endpoint: str, data: dict[str, Any] | None = None, timeout: int = 30) -> dict:
    """Make an HTTP call to the bridge server.

    Args:
        endpoint: Bridge endpoint path (e.g., '/things/inbox')
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


def inbox(limit: int = 50) -> dict[str, Any]:
    """Get Things 3 inbox via bridge."""
    return call_bridge("/things/inbox", {"limit": limit})


def today() -> dict[str, Any]:
    """Get Things 3 today list via bridge."""
    return call_bridge("/things/today")


def upcoming() -> dict[str, Any]:
    """Get Things 3 upcoming list via bridge."""
    return call_bridge("/things/upcoming")


def search(query: str) -> dict[str, Any]:
    """Search Things 3 via bridge."""
    return call_bridge("/things/search", {"query": query})


def projects() -> dict[str, Any]:
    """Get Things 3 projects via bridge."""
    return call_bridge("/things/projects")


def add_item(
    title: str,
    notes: str | None = None,
    tags: str | None = None,
    when: str | None = None,
    list_name: str | None = None,
    heading: str | None = None,
) -> dict[str, Any]:
    """Add item to Things 3 via bridge."""
    data = {"title": title}
    if notes:
        data["notes"] = notes
    if tags:
        data["tags"] = tags
    if when:
        data["when"] = when
    if list_name:
        data["list"] = list_name
    if heading:
        data["heading"] = heading

    return call_bridge("/things/add", data)


def update_item(
    item_id: str,
    completed: bool | None = None,
    title: str | None = None,
    notes: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    """Update Things 3 item via bridge."""
    data = {"id": item_id}
    if completed is not None:
        data["completed"] = completed
    if title:
        data["title"] = title
    if notes:
        data["notes"] = notes
    if tags:
        data["tags"] = tags

    return call_bridge("/things/update", data)


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "inbox")

    try:
        if action == "inbox":
            limit = int(os.environ.get("LIMIT", "50"))
            result = inbox(limit)

        elif action == "today":
            result = today()

        elif action == "upcoming":
            result = upcoming()

        elif action == "search":
            query = os.environ.get("QUERY")
            if not query:
                raise ValueError("QUERY environment variable required for search action")
            result = search(query)

        elif action == "projects":
            result = projects()

        elif action == "add":
            title = os.environ.get("TITLE")
            if not title:
                raise ValueError("TITLE environment variable required for add action")

            notes = os.environ.get("NOTES")
            tags = os.environ.get("TAGS")
            when = os.environ.get("WHEN")
            list_name = os.environ.get("LIST")
            heading = os.environ.get("HEADING")

            result = add_item(title, notes, tags, when, list_name, heading)

        elif action == "update":
            item_id = os.environ.get("ID")
            if not item_id:
                raise ValueError("ID environment variable required for update action")

            completed = os.environ.get("COMPLETED")
            completed_bool = None
            if completed is not None:
                completed_bool = completed.lower() in ("true", "1", "yes")

            title = os.environ.get("TITLE")
            notes = os.environ.get("NOTES")
            tags = os.environ.get("TAGS")

            result = update_item(item_id, completed_bool, title, notes, tags)

        else:
            raise ValueError(f"Unknown action: {action}")

        # Output the bridge response output
        print(result.get("output", ""))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
