#!/usr/bin/env python3
"""Apple Photos executor - bridge-calling executor for macOS Photos app.

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
    """Register the apple_photos skill with the skill registry."""
    import sys
    from io import StringIO
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="apple_photos",
        label="Apple Photos",
        tools=(
            ToolSpec(
                name="recent_photos",
                description="Get recent photos metadata from Apple Photos",
                params=(
                    Param(
                        name="count",
                        type="integer",
                        description="Number of recent photos to retrieve (1-100)",
                    ),
                ),
                fixed_args={"action": "recent"},
            ),
            ToolSpec(
                name="search_photos",
                description="Search photos in Apple Photos by keyword and/or date range",
                params=(
                    Param(
                        name="keyword",
                        type="string",
                        description="Search keyword (e.g. 'beach', 'dog', 'birthday')",
                    ),
                    Param(
                        name="date_from",
                        type="string",
                        description="Start date filter (ISO format: YYYY-MM-DD)",
                    ),
                    Param(
                        name="date_to",
                        type="string",
                        description="End date filter (ISO format: YYYY-MM-DD)",
                    ),
                    Param(
                        name="count",
                        type="integer",
                        description="Maximum results to return (1-100)",
                    ),
                ),
                fixed_args={"action": "search"},
            ),
        ),
        needs_bridge=True,
        needs_network=True,
        bridge_scope="PHOTOS",
        platform="darwin",
    )

    def execute(config: ExecutorConfig) -> str:
        from creel.orchestrator import _env_override

        env_vars = {
            k: v
            for k, v in {
                "ACTION": config.args.get("action", "recent"),
                "COUNT": str(config.args.get("count", "")),
                "KEYWORD": config.args.get("keyword", ""),
                "DATE_FROM": config.args.get("date_from", ""),
                "DATE_TO": config.args.get("date_to", ""),
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
        endpoint: Bridge endpoint path (e.g., '/photos/recent')
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


def recent_photos(count: int = 10) -> dict[str, Any]:
    """Get recent photos metadata via bridge."""
    return call_bridge("/photos/recent", {"count": count})


def search_photos(
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    count: int = 20,
) -> dict[str, Any]:
    """Search photos via bridge."""
    data: dict[str, Any] = {"count": count}
    if keyword:
        data["keyword"] = keyword
    if date_from:
        data["date_from"] = date_from
    if date_to:
        data["date_to"] = date_to

    return call_bridge("/photos/search", data)


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "recent")

    try:
        if action == "recent":
            count = int(os.environ.get("COUNT", "10"))
            result = recent_photos(count)

        elif action == "search":
            keyword = os.environ.get("KEYWORD")
            date_from = os.environ.get("DATE_FROM")
            date_to = os.environ.get("DATE_TO")
            count = int(os.environ.get("COUNT", "20"))

            result = search_photos(keyword, date_from, date_to, count)

        else:
            raise ValueError(f"Unknown action: {action}")

        print(result.get("output", ""))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
