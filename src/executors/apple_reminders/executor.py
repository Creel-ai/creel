#!/usr/bin/env python3
"""Apple Reminders executor - bridge-calling executor for macOS Reminders app.

Instead of running AppleScript directly, this executor makes HTTP calls
to the bridge server which executes remindctl CLI commands on the host.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def register_skill():
    """Register the apple_reminders skill with the skill registry."""
    import sys
    from io import StringIO
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="apple_reminders",
        label="Apple Reminders",
        tools=(
            ToolSpec(
                name="list_reminders",
                description="List reminders from Apple Reminders",
                params=(
                    Param(
                        name="list_name",
                        type="string",
                        description="Reminder list name",
                    ),
                    Param(
                        name="filter",
                        type="string",
                        description="Filter type (e.g. all, incomplete, completed)",
                    ),
                ),
                fixed_args={"action": "list"},
            ),
            ToolSpec(
                name="create_reminder",
                description="Create a new reminder",
                params=(
                    Param(
                        name="title",
                        type="string",
                        description="Title of the reminder",
                        required=True,
                    ),
                    Param(
                        name="due_date",
                        type="string",
                        description="Due date for the reminder",
                    ),
                    Param(
                        name="notes",
                        type="string",
                        description="Notes for the reminder",
                    ),
                    Param(
                        name="list_name",
                        type="string",
                        description="Reminder list name",
                    ),
                ),
                fixed_args={"action": "add"},
            ),
            ToolSpec(
                name="complete_reminder",
                description="Complete a reminder",
                params=(
                    Param(
                        name="name",
                        type="string",
                        description="Name of the reminder to complete",
                        required=True,
                    ),
                    Param(
                        name="list_name",
                        type="string",
                        description="Reminder list name",
                    ),
                ),
                fixed_args={"action": "complete"},
            ),
            ToolSpec(
                name="get_reminder_lists",
                description="Get all reminder lists",
                fixed_args={"action": "lists"},
            ),
        ),
        needs_bridge=True,
        bridge_scope="REMINDERS",
        platform="darwin",
    )

    def execute(config: ExecutorConfig) -> str:
        from creel.orchestrator import _env_override

        env_vars = {
            k: v
            for k, v in {
                "ACTION": config.args.get("action", "list"),
                "FILTER": config.args.get("filter", "all"),
                "TITLE": config.args.get("title", ""),
                "LIST": config.args.get("list_name", ""),
                "DUE": config.args.get("due_date", ""),
                "ID": config.args.get("id", ""),
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
        endpoint: Bridge endpoint path (e.g., '/reminders/list')
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


def list_reminders(filter_type: str = "all") -> dict[str, Any]:
    """List reminders via bridge."""
    return call_bridge("/reminders/list", {"filter": filter_type})


def add_reminder(
    title: str, list_name: str | None = None, due: str | None = None
) -> dict[str, Any]:
    """Add a reminder via bridge."""
    data = {"title": title}
    if list_name:
        data["list"] = list_name
    if due:
        data["due"] = due

    return call_bridge("/reminders/add", data)


def get_reminder_lists() -> dict[str, Any]:
    """List available reminder lists via bridge."""
    return call_bridge("/reminders/lists")


def complete_reminder(reminder_id: str) -> dict[str, Any]:
    """Complete a reminder via bridge."""
    return call_bridge("/reminders/complete", {"id": reminder_id})


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "list")

    try:
        if action == "list":
            filter_type = os.environ.get("FILTER", "all")
            result = list_reminders(filter_type)

        elif action == "add":
            title = os.environ.get("TITLE")
            list_name = os.environ.get("LIST")
            due = os.environ.get("DUE")

            if not title:
                raise ValueError("TITLE environment variable required for add action")

            result = add_reminder(title, list_name, due)

        elif action == "lists":
            result = get_reminder_lists()

        elif action == "complete":
            reminder_id = os.environ.get("ID")
            if not reminder_id:
                raise ValueError("ID environment variable required for complete action")

            result = complete_reminder(reminder_id)

        else:
            raise ValueError(f"Unknown action: {action}")

        # Output the bridge response output
        print(result.get("output", ""))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
