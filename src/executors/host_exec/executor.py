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

import httpx


def register_skill():
    """Register the host_exec skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="host_exec",
        label="Host Exec",
        tools=(
            ToolSpec(
                name="host_exec",
                description="Run a command on the host (foreground or background)",
                params=(
                    Param(
                        name="command",
                        type="string",
                        description="Shell command to execute",
                        required=True,
                    ),
                    Param(
                        name="background",
                        type="string",
                        description="Run in background and return session info (true/false, default false)",
                    ),
                    Param(
                        name="workdir",
                        type="string",
                        description="Working directory for the command",
                    ),
                    Param(
                        name="timeout",
                        type="string",
                        description="Timeout in seconds (default 300)",
                    ),
                    Param(
                        name="env",
                        type="string",
                        description="Additional environment variables as JSON object",
                    ),
                ),
                fixed_args={"_action": "exec"},
            ),
            ToolSpec(
                name="host_process",
                description="Manage a running background process (log, poll, write, kill)",
                params=(
                    Param(
                        name="session_id",
                        type="string",
                        description="Session identifier",
                        required=True,
                    ),
                    Param(
                        name="action",
                        type="string",
                        description="Action to perform: log, poll, write, or kill",
                    ),
                    Param(
                        name="limit",
                        type="string",
                        description="Max log lines to return (default 100)",
                    ),
                    Param(
                        name="offset",
                        type="string",
                        description="Log line offset (default 0)",
                    ),
                    Param(
                        name="data",
                        type="string",
                        description="Data to write to stdin (for write action)",
                    ),
                ),
                fixed_args={"_action": "process"},
            ),
            ToolSpec(
                name="host_sessions",
                description="List all active host sessions",
                params=(),
                fixed_args={"_action": "sessions"},
            ),
        ),
        needs_bridge=True,
        bridge_scope="EXEC",
    )

    def execute(config: ExecutorConfig) -> str:
        action = config.args.get("_action", "")

        if action == "exec":
            command = config.args.get("command", "")
            if not command:
                raise ValueError("host_exec requires a 'command' argument")
            background = config.args.get("background", "false").lower() in (
                "true",
                "1",
                "yes",
            )
            workdir = config.args.get("workdir")
            timeout = int(config.args.get("timeout", "300"))
            env_str = config.args.get("env")
            env = json.loads(env_str) if env_str else None
            if env is not None and not isinstance(env, dict):
                raise ValueError("env must be a JSON object")

            result = host_exec(
                command, background=background, workdir=workdir, timeout=timeout, env=env
            )
        elif action == "process":
            session_id = config.args.get("session_id", "")
            if not session_id:
                raise ValueError("host_process requires a 'session_id' argument")
            sub_action = config.args.get("action", "poll")
            limit = int(config.args.get("limit", "100"))
            offset = int(config.args.get("offset", "0"))
            stdin_data = config.args.get("data")
            result = host_process(
                session_id, sub_action, limit=limit, offset=offset, data=stdin_data
            )
        elif action == "sessions":
            result = host_sessions()
        else:
            raise ValueError(f"host_exec: unknown action '{action}' (use exec/process/sessions)")

        return json.dumps(result, indent=2)

    return meta, execute


# Env vars that callers are never allowed to set — mirrors bridge/process_manager.py
_BLOCKED_ENV_VARS = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "BASH_ENV",
        "ENV",
        "CDPATH",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "PERL5OPT",
        "RUBYOPT",
    }
)
_BLOCKED_ENV_PREFIXES = ("BRIDGE_TOKEN_", "BASH_FUNC_")


def _validate_env(env: dict[str, str]) -> None:
    """Reject dangerous environment variables before sending to bridge.

    Defense-in-depth: the bridge also validates, but we reject locally
    so malicious env vars never leave the executor process.
    """
    for key in env:
        upper = key.upper()
        if upper in _BLOCKED_ENV_VARS:
            raise ValueError(f"Environment variable {key!r} is blocked for security reasons")
        for prefix in _BLOCKED_ENV_PREFIXES:
            if upper.startswith(prefix):
                raise ValueError(f"Environment variable {key!r} is blocked for security reasons")


def call_bridge(
    endpoint: str,
    data: dict[str, Any] | None = None,
    method: str = "POST",
    timeout: int = 600,
) -> dict[str, Any]:
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
            response = httpx.get(url, headers=headers, timeout=timeout)
        else:
            response = httpx.post(url, json=data or {}, headers=headers, timeout=timeout)

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Bridge error: {result.get('error', 'Unknown error')}")

        return result

    except httpx.HTTPError as e:
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
        _validate_env(env)
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


def _load_args_from_input_file() -> None:
    """Load executor args from the JSON input file into env vars."""
    input_file = os.environ.get("CREEL_INPUT_FILE", "")
    if not input_file or not os.path.isfile(input_file):
        return
    import json as _json

    with open(input_file, encoding="utf-8") as f:
        args: dict = _json.load(f)
    for key, value in args.items():
        env_key = key.upper()
        os.environ[env_key] = str(value)


def main() -> None:
    """Main executor entry point."""
    _load_args_from_input_file()
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
