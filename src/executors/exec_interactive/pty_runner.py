#!/usr/bin/env python3
"""Container-side JSON-over-stdio server for the interactive PTY executor.

Runs inside a Docker container, communicating with the host via the same
JSON-over-stdio protocol used by dev_runner.py / ContainerPool:

  Host -> Container (stdin):
    {"type": "ping"}                                  -> {"type": "pong"}
    {"type": "shutdown"}                              -> (exit)
    {"type": "start", "command": "...", ...}          -> {"type": "started", ...}
    {"type": "send_input", "session_id": "...", ...}  -> {"type": "output", ...}
    {"type": "read_output", "session_id": "...", ...} -> {"type": "output", ...}
    {"type": "resize", "session_id": "...", ...}      -> {"type": "resized", ...}
    {"type": "close", "session_id": "..."}            -> {"type": "closed", ...}
    {"type": "info", "session_id": "..."}             -> {"type": "session_info", ...}
    {"type": "get_io_log", "session_id": "..."}       -> {"type": "io_log", ...}

One session per container -- container exits after close or shutdown.
"""

from __future__ import annotations

import json
import sys

try:
    from executor import (
        close_session,
        get_io_log,
        get_session_info,
        list_sessions,
        read_output,
        resize_terminal,
        send_input,
        start_session,
    )
except ImportError:
    from executors.exec_interactive.executor import (
        close_session,
        get_io_log,
        get_session_info,
        list_sessions,
        read_output,
        resize_terminal,
        send_input,
        start_session,
    )


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


def main() -> None:
    """Main message loop -- one session per container."""
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

        if msg_type == "start":
            command = msg.get("command", "")
            timeout = int(msg.get("timeout", 300))
            cols = int(msg.get("cols", 120))
            rows = int(msg.get("rows", 40))
            result = start_session(command, timeout=timeout, cols=cols, rows=rows)
            _send({"type": "started", **result})
            continue

        if msg_type == "send_input":
            session_id = msg.get("session_id", "")
            data = msg.get("input", "")
            result = send_input(session_id, data)
            _send({"type": "output", **result})
            continue

        if msg_type == "read_output":
            session_id = msg.get("session_id", "")
            read_timeout = float(msg.get("read_timeout", 10))
            result = read_output(session_id, timeout=read_timeout)
            _send({"type": "output", **result})
            continue

        if msg_type == "resize":
            session_id = msg.get("session_id", "")
            cols = int(msg.get("cols", 120))
            rows = int(msg.get("rows", 40))
            result = resize_terminal(session_id, cols, rows)
            _send({"type": "resized", **result})
            continue

        if msg_type == "close":
            session_id = msg.get("session_id", "")
            result = close_session(session_id)
            _send({"type": "closed", **result})
            # One session per container -- exit after close
            break

        if msg_type == "info":
            session_id = msg.get("session_id", "")
            result = get_session_info(session_id)
            _send({"type": "session_info", **result})
            continue

        if msg_type == "get_io_log":
            session_id = msg.get("session_id", "")
            result = get_io_log(session_id)
            _send({"type": "io_log", **result})
            continue

        if msg_type == "list_sessions":
            result = list_sessions()
            _send({"type": "sessions_list", **result})
            continue

        _send({"type": "error", "message": f"Unknown message type: {msg_type}"})


if __name__ == "__main__":
    main()
