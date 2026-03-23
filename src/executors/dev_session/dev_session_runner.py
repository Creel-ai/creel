#!/usr/bin/env python3
"""In-container runner for dev session process management.

Runs inside a persistent Docker container, communicating with the host
via JSON-over-stdio.  Wraps a ProcessManager instance that manages
multiple concurrent foreground and background processes.

Host -> Container (stdin):
  {"type": "ping"}                                         -> {"type": "pong"}
  {"type": "shutdown"}                                     -> (exit)
  {"type": "exec", "command": "...",
   "background": true/false, "workdir": "...",
   "timeout": 300}                                         -> {"type": "exec_result", ...}
  {"type": "process", "session_id": "...",
   "action": "log|poll|write|kill",
   "limit": 100, "offset": 0, "data": "..."}              -> {"type": "process_result", ...}
  {"type": "sessions"}                                     -> {"type": "sessions_result", ...}

Container -> Host (stdout):
  {"type": "pong"}
  {"type": "exec_result", "session_id": "...", ...}
  {"type": "process_result", ...}
  {"type": "sessions_result", "sessions": [...]}
  {"type": "error", "message": "..."}
"""

from __future__ import annotations

import json
import sys
from typing import Any

try:
    from process_manager import ProcessManager
except ImportError:
    from executors.dev_session.process_manager import ProcessManager


def _send(obj: dict[str, Any]) -> None:
    """Write a JSON line to stdout."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _recv() -> dict[str, Any]:
    """Read a JSON line from stdin."""
    line = sys.stdin.readline()
    if not line:
        raise EOFError("stdin closed")
    return json.loads(line)


def _handle_exec(pm: ProcessManager, msg: dict[str, Any]) -> dict[str, Any]:
    """Handle an exec request."""
    command = msg.get("command", "")
    if not command:
        return {"type": "error", "message": "exec requires a 'command' field"}

    background = msg.get("background", False)
    workdir = msg.get("workdir")
    timeout = int(msg.get("timeout", 300))

    result = pm.spawn(
        command=command,
        background=background,
        workdir=workdir,
        timeout=timeout,
    )
    return {"type": "exec_result", **result}


def _handle_process(pm: ProcessManager, msg: dict[str, Any]) -> dict[str, Any]:
    """Handle a process management request."""
    session_id = msg.get("session_id", "")
    if not session_id:
        return {"type": "error", "message": "process requires a 'session_id' field"}

    action = msg.get("action", "poll")

    if action == "poll":
        result = pm.poll(session_id)
        return {"type": "process_result", **result}

    if action == "log":
        limit = int(msg.get("limit", 100))
        offset = int(msg.get("offset", 0))
        lines = pm.log(session_id, limit=limit, offset=offset)
        return {"type": "process_result", "session_id": session_id, "lines": lines}

    if action == "write":
        data = msg.get("data")
        if data is None:
            return {"type": "error", "message": "write action requires 'data' field"}
        result = pm.write(session_id, data)
        return {"type": "process_result", **result}

    if action == "kill":
        result = pm.kill(session_id)
        return {"type": "process_result", **result}

    return {"type": "error", "message": f"Unknown process action: {action}"}


def main() -> None:
    """Main protocol loop."""
    pm = ProcessManager(
        max_sessions=10,
        max_age_hours=4,
        buffer_lines=2000,
    )

    try:
        while True:
            try:
                msg = _recv()
            except (EOFError, json.JSONDecodeError):
                break

            msg_type = msg.get("type")

            if msg_type == "ping":
                _send({"type": "pong"})
                continue

            if msg_type == "shutdown":
                break

            if msg_type == "exec":
                try:
                    _send(_handle_exec(pm, msg))
                except Exception as e:
                    _send({"type": "error", "message": str(e)})
                continue

            if msg_type == "process":
                try:
                    _send(_handle_process(pm, msg))
                except (KeyError, RuntimeError, ValueError) as e:
                    _send({"type": "error", "message": str(e)})
                continue

            if msg_type == "sessions":
                _send({"type": "sessions_result", "sessions": pm.list_sessions()})
                continue

            _send({"type": "error", "message": f"Unknown message type: {msg_type}"})
    finally:
        pm.shutdown()


if __name__ == "__main__":
    main()
