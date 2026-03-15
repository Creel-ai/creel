"""Host exec executor - bridge-calling executor for host process management.

Provides three tool functions that call bridge endpoints:
- host_exec: spawn commands (foreground or background)
- host_process: manage running processes (log, poll, write, kill)
- host_sessions: list all active sessions
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def call_bridge(
    endpoint: str,
    data: dict[str, Any] | None = None,
    method: str = "POST",
    timeout: int = 600,
) -> dict:
    """Make an HTTP call to the bridge server.

    Args:
        endpoint: Bridge endpoint path (e.g., '/exec')
        data: Request body data (optional)
        method: HTTP method (POST or GET)
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
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=timeout)
        else:
            response = requests.post(url, json=data or {}, headers=headers, timeout=timeout)

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Bridge error: {result.get('error', 'Unknown error')}")

        return result

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Bridge request failed: {e}") from e


def host_exec(
    command: str,
    background: bool = False,
    workdir: str | None = None,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a command on the host via bridge /exec endpoint.

    Args:
        command: Shell command to execute.
        background: If True, run in background and return session info.
        workdir: Working directory for the command.
        timeout: Timeout in seconds.
        env: Additional environment variables.

    Returns:
        Dict with execution results or session info.
    """
    data: dict[str, Any] = {
        "command": command,
        "background": background,
        "timeout": timeout,
    }
    if workdir:
        data["workdir"] = workdir
    if env:
        data["env"] = env

    # Use a longer HTTP timeout for foreground commands
    http_timeout = timeout + 30 if not background else 30
    return call_bridge("/exec", data, timeout=http_timeout)


def host_process(
    session_id: str,
    action: str,
    limit: int = 100,
    offset: int = 0,
    data: str | None = None,
) -> dict[str, Any]:
    """Manage a running process via bridge /process endpoint.

    Args:
        session_id: Session identifier.
        action: Action to perform (log, poll, write, kill).
        limit: Max log lines to return (for log action).
        offset: Log line offset (for log action).
        data: Data to write to stdin (for write action).

    Returns:
        Dict with action results.
    """
    payload: dict[str, Any] = {
        "session_id": session_id,
        "action": action,
        "limit": limit,
        "offset": offset,
    }
    if data is not None:
        payload["data"] = data

    return call_bridge("/process", payload)


def host_sessions() -> dict[str, Any]:
    """List all active sessions via bridge /sessions endpoint.

    Returns:
        Dict with list of session info dicts.
    """
    return call_bridge("/sessions", method="GET")


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "exec")

    try:
        if action == "exec":
            command = os.environ.get("COMMAND", "")
            if not command:
                raise ValueError("COMMAND environment variable required")
            background = os.environ.get("BACKGROUND", "false").lower() in ("true", "1", "yes")
            workdir = os.environ.get("WORKDIR")
            timeout = int(os.environ.get("TIMEOUT", "300"))
            result = host_exec(command, background=background, workdir=workdir, timeout=timeout)

        elif action == "process":
            session_id = os.environ.get("SESSION_ID", "")
            if not session_id:
                raise ValueError("SESSION_ID environment variable required")
            sub_action = os.environ.get("SUB_ACTION", "poll")
            limit = int(os.environ.get("LIMIT", "100"))
            offset = int(os.environ.get("OFFSET", "0"))
            stdin_data = os.environ.get("DATA")
            result = host_process(
                session_id, sub_action, limit=limit, offset=offset, data=stdin_data
            )

        elif action == "sessions":
            result = host_sessions()

        else:
            raise ValueError(f"Unknown action: {action}")

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
