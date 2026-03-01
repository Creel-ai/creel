#!/usr/bin/env python3
"""Git operations executor - bridge-calling executor for git commands.

Instead of running git directly, this executor makes HTTP calls
to the bridge server which executes git commands on the host.
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
        endpoint: Bridge endpoint path (e.g., '/git/status')
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


def status(short: bool = False) -> dict[str, Any]:
    """Get git status via bridge."""
    data = {}
    if short:
        data["short"] = True
    return call_bridge("/git/status", data)


def diff(cached: bool = False, path: str | None = None) -> dict[str, Any]:
    """Get git diff via bridge."""
    data: dict[str, Any] = {}
    if cached:
        data["cached"] = True
    if path:
        data["path"] = path
    return call_bridge("/git/diff", data)


def log(max_count: int = 10, oneline: bool = True) -> dict[str, Any]:
    """Get git log via bridge."""
    return call_bridge("/git/log", {"max_count": max_count, "oneline": oneline})


def commit(message: str, all: bool = False) -> dict[str, Any]:
    """Create a git commit via bridge."""
    data: dict[str, Any] = {"message": message}
    if all:
        data["all"] = True
    return call_bridge("/git/commit", data)


def branch(name: str | None = None, delete: bool = False, list_all: bool = False) -> dict[str, Any]:
    """List or manage git branches via bridge."""
    data: dict[str, Any] = {}
    if name:
        data["name"] = name
    if delete:
        data["delete"] = True
    if list_all:
        data["list_all"] = True
    return call_bridge("/git/branch", data)


def push(
    remote: str = "origin", branch_name: str | None = None, set_upstream: bool = False
) -> dict[str, Any]:
    """Push to remote via bridge."""
    data: dict[str, Any] = {"remote": remote}
    if branch_name:
        data["branch"] = branch_name
    if set_upstream:
        data["set_upstream"] = True
    return call_bridge("/git/push", data, timeout=60)


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "status")

    try:
        if action == "status":
            short = os.environ.get("SHORT", "").lower() in ("true", "1", "yes")
            result = status(short)

        elif action == "diff":
            cached = os.environ.get("CACHED", "").lower() in ("true", "1", "yes")
            path = os.environ.get("PATH_FILTER") or os.environ.get("DIFF_PATH")
            result = diff(cached, path)

        elif action == "log":
            max_count = int(os.environ.get("MAX_COUNT", "10"))
            oneline = os.environ.get("ONELINE", "true").lower() in ("true", "1", "yes")
            result = log(max_count, oneline)

        elif action == "commit":
            message = os.environ.get("MESSAGE")
            if not message:
                raise ValueError("MESSAGE environment variable required for commit action")
            commit_all = os.environ.get("ALL", "").lower() in ("true", "1", "yes")
            result = commit(message, commit_all)

        elif action == "branch":
            name = os.environ.get("BRANCH_NAME")
            delete = os.environ.get("DELETE", "").lower() in ("true", "1", "yes")
            list_all = os.environ.get("LIST_ALL", "").lower() in ("true", "1", "yes")
            result = branch(name, delete, list_all)

        elif action == "push":
            remote = os.environ.get("REMOTE", "origin")
            branch_name = os.environ.get("BRANCH_NAME")
            set_upstream = os.environ.get("SET_UPSTREAM", "").lower() in (
                "true",
                "1",
                "yes",
            )
            result = push(remote, branch_name, set_upstream)

        else:
            raise ValueError(f"Unknown action: {action}")

        # Output the bridge response output
        print(result.get("output", ""))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
