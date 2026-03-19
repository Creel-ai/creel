"""Interactive/PTY exec executor — runs commands in a pseudo-terminal.

Supports interactive commands (SSH, REPLs, editors) with:
- PTY allocation and terminal size negotiation
- Input/output streaming
- Session lifecycle management (start, interact, close)
- Hard timeout enforcement
- Audit logging of all I/O
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
import select
import signal
import struct
import termios
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default terminal dimensions
DEFAULT_COLS = 120
DEFAULT_ROWS = 40
DEFAULT_TIMEOUT = 300  # 5 minutes
MAX_TIMEOUT = 3600  # 1 hour hard cap
MAX_SESSIONS = 10  # Max concurrent PTY sessions

# Environment variables safe to inherit into PTY sessions.
# All others are stripped to prevent credential leakage.
_SAFE_ENV_VARS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TERM",
        "TMPDIR",
        "TZ",
    }
)
_SAFE_ENV_PREFIXES = ("LC_",)

# Command patterns rejected at both start_session and send_input time.
# Mirrors the patterns in bridge/process_manager.py for defense-in-depth.
_BLOCKED_COMMAND_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+.*(?:-\w*r\w*f|-\w*f\w*r|--recursive|--force)", re.IGNORECASE),
    re.compile(r"\brm\s+.*-r\b.*-f\b|\brm\s+.*-f\b.*-r\b", re.IGNORECASE),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\b.*\bof\s*=\s*/dev/"),
    re.compile(r">\s*/dev/sd[a-z]|>\s*/dev/nvme"),
    re.compile(r":\(\)\s*\{.*:\s*\|\s*:.*&.*\}\s*;\s*:"),  # fork bomb
    re.compile(r"\bchmod\s+.*777\s+/"),
    re.compile(r"\bcurl\b.*\|\s*(?:ba)?sh\b"),
    re.compile(r"\bwget\b.*\|\s*(?:ba)?sh\b"),
    re.compile(r"/dev/tcp/|/dev/udp/"),  # reverse shells
    re.compile(r"\bnc\b.*-[el]|\bncat\b.*-[el]"),  # bind shells
    re.compile(r"\bsudo\b"),
    re.compile(r"\bpython3?\s+-c\b"),
    re.compile(r"\bperl\s+-e\b"),
    re.compile(r"\bruby\s+-e\b"),
    re.compile(r"\bosascript\b"),
    re.compile(r"\beval\s+\$\("),
]

# Max bytes to read from PTY in a single call
READ_CHUNK_SIZE = 4096

# Max accumulated output size before truncation (1 MB)
MAX_OUTPUT_BYTES = 1_048_576


@dataclass
class InteractiveSession:
    """Represents an active PTY session."""

    session_id: str
    pid: int
    fd: int
    command: str
    cols: int
    rows: int
    timeout: int
    started_at: float
    io_log: list[dict] = field(default_factory=list)
    _closed: bool = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def timed_out(self) -> bool:
        return self.elapsed >= self.timeout

    @property
    def closed(self) -> bool:
        return self._closed


# Global session registry
_sessions: dict[str, InteractiveSession] = {}
_sessions_lock = threading.Lock()


def _validate_command(text: str) -> str | None:
    """Check text against blocked command patterns.

    Returns an error message if blocked, or None if safe.
    Used for both initial commands and send_input data.
    """
    for pattern in _BLOCKED_COMMAND_PATTERNS:
        if pattern.search(text):
            return "Command rejected by safety filter"
    return None


def _build_safe_env() -> dict[str, str]:
    """Build a minimal environment dict for PTY child processes.

    Strips all credentials and sensitive variables, inheriting only
    safe vars like PATH, HOME, LANG, etc.
    """
    safe: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_VARS or any(key.startswith(p) for p in _SAFE_ENV_PREFIXES):
            safe[key] = value
    # Ensure TERM is set for PTY
    safe.setdefault("TERM", "xterm-256color")
    return safe


def _set_terminal_size(fd: int, cols: int, rows: int) -> None:
    """Set the terminal size on a PTY file descriptor."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _read_available(fd: int, timeout: float = 0.1) -> bytes:
    """Read all available data from the PTY fd within a timeout.

    Returns whatever data is available, or empty bytes if nothing
    arrives within the timeout period.
    """
    output = b""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.05))
        if ready:
            try:
                chunk = os.read(fd, READ_CHUNK_SIZE)
                if not chunk:
                    break  # EOF
                output += chunk
                if len(output) >= MAX_OUTPUT_BYTES:
                    break
            except OSError as e:
                if e.errno == errno.EIO:
                    break  # PTY closed
                raise
        else:
            # No data ready and we've already collected some — return it
            if output:
                break
    return output


def start_session(
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
) -> dict:
    """Start an interactive PTY session.

    Args:
        command: Shell command to execute in the PTY.
        timeout: Hard timeout in seconds (default 300).
        cols: Terminal width in columns.
        rows: Terminal height in rows.

    Returns:
        Dict with session_id, status, and initial output.
    """
    if not command:
        return {
            "success": False,
            "error": "No command provided",
        }

    if timeout <= 0:
        return {
            "success": False,
            "error": "Timeout must be positive",
        }

    # Cap timeout to prevent indefinite sessions
    timeout = min(timeout, MAX_TIMEOUT)

    # Bound terminal dimensions
    cols = max(10, min(cols, 500))
    rows = max(5, min(rows, 200))

    # Enforce max concurrent sessions
    active_count = sum(1 for s in _sessions.values() if not s.closed)
    if active_count >= MAX_SESSIONS:
        return {
            "success": False,
            "error": f"Maximum concurrent sessions ({MAX_SESSIONS}) reached. Close an existing session first.",
        }

    # Validate command against blocklist
    rejection = _validate_command(command)
    if rejection:
        return {
            "success": False,
            "error": rejection,
        }

    session_id = uuid.uuid4().hex[:12]
    safe_env = _build_safe_env()

    try:
        pid, fd = os.forkpty()
    except OSError as e:
        return {
            "success": False,
            "error": f"Failed to allocate PTY: {e}",
        }

    if pid == 0:
        # Child process — exec the command in a shell with sanitized env
        try:
            os.execve("/bin/bash", ["bash", "-c", command], safe_env)  # noqa: S606
        except Exception:
            os._exit(127)

    # Parent process
    _set_terminal_size(fd, cols, rows)

    # Set non-blocking mode
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    session = InteractiveSession(
        session_id=session_id,
        pid=pid,
        fd=fd,
        command=command,
        cols=cols,
        rows=rows,
        timeout=timeout,
        started_at=time.monotonic(),
    )
    _sessions[session_id] = session

    # Read initial output (give process a moment to start)
    initial_output = _read_available(fd, timeout=1.0)
    decoded = initial_output.decode("utf-8", errors="replace")

    session.io_log.append(
        {
            "direction": "output",
            "data": decoded,
            "ts": time.time(),
        }
    )

    logger.info(
        "Started interactive session %s: command=%r, cols=%d, rows=%d, timeout=%d",
        session_id,
        command,
        cols,
        rows,
        timeout,
    )

    return {
        "success": True,
        "session_id": session_id,
        "command": command,
        "cols": cols,
        "rows": rows,
        "timeout": timeout,
        "initial_output": decoded,
    }


def send_input(session_id: str, data: str) -> dict:
    """Send input to a running interactive session.

    Args:
        session_id: The session to send input to.
        data: String data to write to the PTY (include \\n for Enter).

    Returns:
        Dict with success status and any immediate output.
    """
    session = _sessions.get(session_id)
    if session is None:
        return {
            "success": False,
            "error": f"Session not found: {session_id}",
        }

    if session.closed:
        return {
            "success": False,
            "error": f"Session {session_id} is already closed",
        }

    if session.timed_out:
        _force_close(session)
        return {
            "success": False,
            "error": f"Session {session_id} timed out after {session.timeout}s",
        }

    # Validate input against blocked command patterns
    rejection = _validate_command(data)
    if rejection:
        return {
            "success": False,
            "error": rejection,
        }

    # Cap io_log to prevent unbounded memory growth
    if len(session.io_log) > 10_000:
        session.io_log = session.io_log[-5_000:]

    session.io_log.append(
        {
            "direction": "input",
            "data": data,
            "ts": time.time(),
        }
    )

    try:
        os.write(session.fd, data.encode("utf-8"))
    except OSError as e:
        if e.errno == errno.EIO:
            _force_close(session)
            return {
                "success": False,
                "error": f"Session {session_id} has ended (process exited)",
            }
        return {
            "success": False,
            "error": f"Write failed: {e}",
        }

    # Read response (allow some time for the command to produce output)
    output = _read_available(session.fd, timeout=1.0)
    decoded = output.decode("utf-8", errors="replace")

    if decoded:
        session.io_log.append(
            {
                "direction": "output",
                "data": decoded,
                "ts": time.time(),
            }
        )

    return {
        "success": True,
        "session_id": session_id,
        "output": decoded,
    }


def read_output(session_id: str, timeout: float = 10.0) -> dict:
    """Read available output from a running interactive session.

    Args:
        session_id: The session to read from.
        timeout: How long to wait for output (seconds).

    Returns:
        Dict with success status and output data.
    """
    session = _sessions.get(session_id)
    if session is None:
        return {
            "success": False,
            "error": f"Session not found: {session_id}",
        }

    if session.closed:
        return {
            "success": False,
            "error": f"Session {session_id} is already closed",
        }

    if session.timed_out:
        _force_close(session)
        return {
            "success": False,
            "error": f"Session {session_id} timed out after {session.timeout}s",
        }

    # Cap the read timeout to remaining session time
    remaining = session.timeout - session.elapsed
    effective_timeout = min(timeout, remaining)

    output = _read_available(session.fd, timeout=effective_timeout)
    decoded = output.decode("utf-8", errors="replace")

    if decoded:
        session.io_log.append(
            {
                "direction": "output",
                "data": decoded,
                "ts": time.time(),
            }
        )

    return {
        "success": True,
        "session_id": session_id,
        "output": decoded,
        "elapsed": round(session.elapsed, 1),
        "remaining": round(max(0, session.timeout - session.elapsed), 1),
    }


def resize_terminal(session_id: str, cols: int, rows: int) -> dict:
    """Resize the terminal of a running session.

    Args:
        session_id: The session to resize.
        cols: New terminal width.
        rows: New terminal height.

    Returns:
        Dict with success status.
    """
    session = _sessions.get(session_id)
    if session is None:
        return {
            "success": False,
            "error": f"Session not found: {session_id}",
        }

    if session.closed:
        return {
            "success": False,
            "error": f"Session {session_id} is already closed",
        }

    try:
        _set_terminal_size(session.fd, cols, rows)
        session.cols = cols
        session.rows = rows
    except OSError as e:
        return {
            "success": False,
            "error": f"Failed to resize terminal: {e}",
        }

    return {
        "success": True,
        "session_id": session_id,
        "cols": cols,
        "rows": rows,
    }


def close_session(session_id: str) -> dict:
    """Close an interactive session and clean up resources.

    Args:
        session_id: The session to close.

    Returns:
        Dict with success status, exit info, and I/O log summary.
    """
    session = _sessions.get(session_id)
    if session is None:
        return {
            "success": False,
            "error": f"Session not found: {session_id}",
        }

    if session.closed:
        io_summary = _build_io_summary(session)
        return {
            "success": True,
            "session_id": session_id,
            "already_closed": True,
            "io_summary": io_summary,
        }

    return _force_close(session)


def get_session_info(session_id: str) -> dict:
    """Get information about a session.

    Args:
        session_id: The session to query.

    Returns:
        Dict with session metadata.
    """
    session = _sessions.get(session_id)
    if session is None:
        return {
            "success": False,
            "error": f"Session not found: {session_id}",
        }

    return {
        "success": True,
        "session_id": session_id,
        "command": session.command,
        "cols": session.cols,
        "rows": session.rows,
        "timeout": session.timeout,
        "elapsed": round(session.elapsed, 1),
        "remaining": round(max(0, session.timeout - session.elapsed), 1),
        "closed": session.closed,
        "timed_out": session.timed_out,
        "io_events": len(session.io_log),
    }


def list_sessions() -> dict:
    """List all active sessions.

    Returns:
        Dict with list of session summaries.
    """
    sessions = []
    for sid, session in _sessions.items():
        sessions.append(
            {
                "session_id": sid,
                "command": session.command,
                "elapsed": round(session.elapsed, 1),
                "closed": session.closed,
                "timed_out": session.timed_out,
            }
        )

    return {
        "success": True,
        "sessions": sessions,
    }


def get_io_log(session_id: str) -> dict:
    """Get the full I/O log for a session (for audit).

    Args:
        session_id: The session to get the log for.

    Returns:
        Dict with the I/O log entries.
    """
    session = _sessions.get(session_id)
    if session is None:
        return {
            "success": False,
            "error": f"Session not found: {session_id}",
        }

    return {
        "success": True,
        "session_id": session_id,
        "command": session.command,
        "io_log": session.io_log,
    }


def _force_close(session: InteractiveSession) -> dict:
    """Force-close a session, killing the process group and cleaning up."""
    exit_code = None
    try:
        # Kill the entire process group (forkpty children are session leaders)
        os.killpg(session.pid, signal.SIGTERM)
        # Give it a moment to exit
        for _ in range(10):
            pid_result, status = os.waitpid(session.pid, os.WNOHANG)
            if pid_result != 0:
                exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                break
            time.sleep(0.1)
        else:
            # Force kill the process group
            os.killpg(session.pid, signal.SIGKILL)
            _, status = os.waitpid(session.pid, 0)
            exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
    except ChildProcessError:
        exit_code = -1
    except OSError:
        exit_code = -1

    try:
        os.close(session.fd)
    except OSError:
        pass

    session._closed = True

    io_summary = _build_io_summary(session)

    logger.info(
        "Closed interactive session %s: exit_code=%s, io_events=%d, elapsed=%.1fs",
        session.session_id,
        exit_code,
        len(session.io_log),
        session.elapsed,
    )

    return {
        "success": True,
        "session_id": session.session_id,
        "exit_code": exit_code,
        "elapsed": round(session.elapsed, 1),
        "timed_out": session.timed_out,
        "io_summary": io_summary,
    }


def _build_io_summary(session: InteractiveSession) -> dict:
    """Build a summary of session I/O for audit purposes."""
    input_count = sum(1 for e in session.io_log if e["direction"] == "input")
    output_count = sum(1 for e in session.io_log if e["direction"] == "output")
    total_input_bytes = sum(len(e["data"]) for e in session.io_log if e["direction"] == "input")
    total_output_bytes = sum(len(e["data"]) for e in session.io_log if e["direction"] == "output")

    return {
        "input_events": input_count,
        "output_events": output_count,
        "total_input_bytes": total_input_bytes,
        "total_output_bytes": total_output_bytes,
        "total_events": len(session.io_log),
    }


def cleanup_timed_out_sessions() -> list[str]:
    """Clean up any timed-out sessions. Returns list of cleaned session IDs."""
    cleaned = []
    for sid, session in list(_sessions.items()):
        if not session.closed and session.timed_out:
            _force_close(session)
            cleaned.append(sid)
    return cleaned
