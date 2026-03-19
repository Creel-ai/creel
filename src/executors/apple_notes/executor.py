#!/usr/bin/env python3
"""Apple Notes executor - bridge-calling executor for macOS Notes app.

Instead of running AppleScript directly, this executor makes HTTP calls
to the bridge server which executes memo CLI commands on the host.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def register_skill():
    """Register the apple_notes skill with the skill registry."""
    import sys
    from io import StringIO
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="apple_notes",
        label="Apple Notes",
        tools=(
            ToolSpec(
                name="list_notes",
                description="List notes from Apple Notes",
                params=(
                    Param(
                        name="folder",
                        type="string",
                        description="Folder to list notes from",
                    ),
                ),
                fixed_args={"action": "list"},
            ),
            ToolSpec(
                name="search_notes",
                description="Search Apple Notes",
                params=(
                    Param(
                        name="query",
                        type="string",
                        description="Search query",
                        required=True,
                    ),
                ),
                fixed_args={"action": "search"},
            ),
            ToolSpec(
                name="read_note",
                description="Read a note from Apple Notes",
                params=(
                    Param(
                        name="name",
                        type="string",
                        description="Name of the note to read",
                        required=True,
                    ),
                ),
                fixed_args={"action": "read"},
            ),
            ToolSpec(
                name="create_note",
                description="Create a new note in Apple Notes",
                params=(
                    Param(
                        name="title",
                        type="string",
                        description="Title of the note",
                        required=True,
                    ),
                    Param(
                        name="body",
                        type="string",
                        description="Body of the note",
                        required=True,
                    ),
                    Param(
                        name="folder",
                        type="string",
                        description="Folder to create the note in",
                    ),
                ),
                fixed_args={"action": "create"},
            ),
        ),
        needs_bridge=True,
        bridge_scope="NOTES",
        platform="darwin",
    )

    def execute(config: ExecutorConfig) -> str:
        from creel.orchestrator import _env_override

        env_vars = {
            k: v
            for k, v in {
                "ACTION": config.args.get("action", "list"),
                "FOLDER": config.args.get("folder", ""),
                "QUERY": config.args.get("query", ""),
                "TITLE": config.args.get("title", ""),
                "BODY": config.args.get("body", ""),
            }.items()
            if v
        }
        old_stdout = sys.stdout
        try:
            sys.stdout = captured_output = StringIO()
            with _env_override(env_vars):
                main()
            return captured_output.getvalue().strip() or "{}"
        finally:
            sys.stdout = old_stdout

    return meta, execute


def call_bridge(endpoint: str, data: dict[str, Any] | None = None, timeout: int = 30) -> dict:
    """Make an HTTP call to the bridge server.

    Args:
        endpoint: Bridge endpoint path (e.g., '/notes/list')
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


def list_notes(folder: str | None = None) -> dict[str, Any]:
    """List notes via bridge."""
    data = {}
    if folder:
        data["folder"] = folder

    return call_bridge("/notes/list", data if data else None)


def search_notes(query: str) -> dict[str, Any]:
    """Search notes via bridge."""
    return call_bridge("/notes/search", {"query": query})


def create_note(title: str, body: str = "", folder: str | None = None) -> dict[str, Any]:
    """Create a note via bridge."""
    data = {"title": title, "body": body}
    if folder:
        data["folder"] = folder

    return call_bridge("/notes/create", data)


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "list")

    try:
        if action == "list":
            folder = os.environ.get("FOLDER")
            result = list_notes(folder)

        elif action == "search":
            query = os.environ.get("QUERY")
            if not query:
                raise ValueError("QUERY environment variable required for search action")
            result = search_notes(query)

        elif action == "create":
            title = os.environ.get("TITLE")
            body = os.environ.get("BODY", "")
            folder = os.environ.get("FOLDER")

            if not title:
                raise ValueError("TITLE environment variable required for create action")

            result = create_note(title, body, folder)

        elif action == "read":
            # Note: memo CLI doesn't have a direct read by ID function,
            # so we'd need to implement this differently or use search
            raise NotImplementedError("Read action not implemented - use search instead")

        else:
            raise ValueError(f"Unknown action: {action}")

        # Output the bridge response output
        print(result.get("output", ""))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
