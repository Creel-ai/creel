#!/usr/bin/env python3
"""Keepalive dev container runner for the coding executor.

Runs inside a persistent Docker container, communicating with the host
via JSON-over-stdio (same protocol as llm/agent_runner.py):

  Host → Container (stdin):
    {"type": "ping"}                          → {"type": "pong"}
    {"type": "reset"}                         → {"type": "ready"}
    {"type": "shutdown"}                      → (exit)
    {"type": "execute", "command": "...",
     "workdir": "...", "timeout": N}          → {"type": "result", ...}

  Container → Host (stdout):
    {"type": "pong"}
    {"type": "ready"}
    {"type": "result", "command": "...", "exit_code": N,
     "stdout": "...", "stderr": "...", "success": true/false}
    {"type": "error", "message": "..."}

The container stays alive between executions, preserving installed packages
and project state. Reset clears temp files but keeps /usr/local/lib intact.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

try:
    from executor import detect_and_setup, run_command
except ImportError:
    from executors.coding.executor import detect_and_setup, run_command


def _send(obj: dict) -> None:
    """Write a JSON line to stdout."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _recv() -> dict:
    """Read a JSON line from stdin."""
    line = sys.stdin.readline()
    if not line:
        raise EOFError("stdin closed")
    return json.loads(line)


def _reset() -> None:
    """Clear temp files but preserve installed packages."""
    # Clean /tmp
    tmp_dir = tempfile.gettempdir()
    for entry in os.listdir(tmp_dir):
        path = os.path.join(tmp_dir, entry)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
        except OSError:
            pass

    # Clean .creel-setup-done markers so auto-setup can re-run if workspace changes
    for root, _dirs, files in os.walk("/workspace"):
        for f in files:
            if f == ".creel-setup-done":
                try:
                    os.unlink(os.path.join(root, f))
                except OSError:
                    pass
        break  # Only top level


def main() -> None:
    """Main keepalive loop."""
    while True:
        try:
            msg = _recv()
        except (EOFError, json.JSONDecodeError):
            break

        msg_type = msg.get("type")

        if msg_type == "ping":
            _send({"type": "pong"})
            continue

        if msg_type == "reset":
            _reset()
            _send({"type": "ready"})
            continue

        if msg_type == "shutdown":
            break

        if msg_type == "execute":
            command = msg.get("command", "")
            workdir = msg.get("workdir") or None
            timeout = msg.get("timeout")

            if not command:
                _send({"type": "error", "message": "No command provided"})
                continue

            # Auto-detect and install project dependencies on first execute.
            # Setup failures are non-fatal — always proceed to the user's command.
            if workdir:
                try:
                    detect_and_setup(workdir)
                except Exception:
                    pass

            try:
                result = run_command(
                    command=command,
                    workdir=workdir,
                    timeout=timeout,
                )
                _send({"type": "result", **result})
            except Exception as e:
                _send({"type": "error", "message": str(e)})
            continue

        _send({"type": "error", "message": f"Unknown message type: {msg_type}"})


if __name__ == "__main__":
    main()
