"""Process manager for host exec via bridge.

Spawns, manages, and monitors background processes on the host.
Provides ring-buffered output capture and session lifecycle management.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BUFFER_LINES = 2000
DEFAULT_MAX_SESSIONS = 10
DEFAULT_MAX_AGE_HOURS = 4
DEFAULT_TIMEOUT = 300  # 5 minutes for foreground commands
DEFAULT_CLEANUP_INTERVAL = 1800  # 30 minutes

# Minimal set of env vars inherited by spawned processes (C2 fix).
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

# Env var names that callers are never allowed to set (C3 fix).
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
        "BRIDGE_TOKEN_EXEC",
        "BRIDGE_TOKEN_NOTES",
        "BRIDGE_TOKEN_REMINDERS",
        "BRIDGE_TOKEN_THINGS",
        "BRIDGE_TOKEN_IMESSAGE",
        "BRIDGE_TOKEN_BROWSER",
        "BRIDGE_TOKEN_GIT",
    }
)
_BLOCKED_ENV_PREFIXES = ("BRIDGE_TOKEN_", "BASH_FUNC_")

# Command patterns that are unconditionally rejected (C1 fix — defense-in-depth).
_BLOCKED_COMMAND_PATTERNS: list[re.Pattern[str]] = [
    # rm with recursive+force in any flag style (-rf, -fr, --recursive, --force)
    re.compile(r"\brm\s+.*(?:-\w*r\w*f|-\w*f\w*r|--recursive|--force)", re.IGNORECASE),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\b.*\bof\s*=\s*/dev/"),
    re.compile(r">\s*/dev/sd[a-z]|>\s*/dev/nvme"),
    re.compile(r":\(\)\s*\{.*:\s*\|\s*:.*&.*\}\s*;\s*:"),  # fork bomb variants
    re.compile(r"\bchmod\s+.*777\s+/"),
    re.compile(r"\bchown\s+.*-R\s+.*\s+/(?:etc|usr|var|bin|sbin|lib|boot)\b"),
    re.compile(r"\bcurl\b.*\|\s*(?:ba)?sh\b"),
    re.compile(r"\bwget\b.*\|\s*(?:ba)?sh\b"),
    re.compile(r"/dev/tcp/|/dev/udp/"),  # reverse shells
    re.compile(r"\bnc\b.*-[el]|\bncat\b.*-[el]"),  # bind shells
    re.compile(r"\bsudo\b"),
    re.compile(r"(?:^|\s|[;&|])su\s+-?\s*root\b|(?:^|\s|[;&|])su\s*$"),
]


@dataclass
class ProcessSession:
    """A managed process session with buffered output."""

    session_id: str
    pid: int
    command: str
    workdir: str | None
    process: subprocess.Popen[str]
    stdout_buffer: deque[str] = field(default_factory=lambda: deque(maxlen=DEFAULT_BUFFER_LINES))
    stderr_buffer: deque[str] = field(default_factory=lambda: deque(maxlen=DEFAULT_BUFFER_LINES))
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_output_at: datetime | None = None
    status: str = "running"  # running | exited | killed | timeout
    exit_code: int | None = None
    output_lines: int = 0
    _output_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    combined_buffer: deque[str] = field(default_factory=lambda: deque(maxlen=DEFAULT_BUFFER_LINES))

    def to_dict(self) -> dict[str, Any]:
        """Serialize session info for API responses."""
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "command": self.command,
            "workdir": self.workdir,
            "started_at": self.started_at.isoformat(),
            "last_output_at": self.last_output_at.isoformat() if self.last_output_at else None,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout_lines": len(self.stdout_buffer),
            "stderr_lines": len(self.stderr_buffer),
            "output_lines": self.output_lines,
        }


class ProcessManager:
    """Manages spawned processes with output buffering and lifecycle control."""

    def __init__(
        self,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
        buffer_lines: int = DEFAULT_BUFFER_LINES,
        allowed_workdirs: list[str] | None = None,
        cleanup_interval: int = DEFAULT_CLEANUP_INTERVAL,
    ):
        self.max_sessions = max_sessions
        self.max_age_hours = max_age_hours
        self.buffer_lines = buffer_lines
        self.allowed_workdirs = allowed_workdirs
        self._sessions: dict[str, ProcessSession] = {}
        self._counters: dict[str, int] = {}
        self._pending_spawns: int = 0
        self._lock = threading.Lock()
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = self._start_cleanup_thread(cleanup_interval)

    def _generate_session_id(self, command: str) -> str:
        """Generate a human-readable session ID from the command basename."""
        # Extract the first token (command name) and strip path
        parts = command.strip().split()
        if not parts:
            basename = "cmd"
        else:
            basename = os.path.basename(parts[0])
            # Remove common extensions
            for ext in (".py", ".sh", ".js", ".ts"):
                if basename.endswith(ext):
                    basename = basename[: -len(ext)]
                    break
            # Sanitize: keep alphanumerics, hyphens, underscores
            basename = "".join(c if c.isalnum() or c in "-_" else "" for c in basename)
            if not basename:
                basename = "cmd"

        with self._lock:
            self._counters[basename] = self._counters.get(basename, 0) + 1
            return f"{basename}-{self._counters[basename]}"

    def _validate_workdir(self, workdir: str) -> None:
        """Validate workdir against allowed_workdirs prefixes."""
        if not self.allowed_workdirs:
            return
        resolved = os.path.realpath(workdir)
        for prefix in self.allowed_workdirs:
            resolved_prefix = os.path.realpath(prefix)
            if resolved == resolved_prefix or resolved.startswith(resolved_prefix + os.sep):
                return
        raise ValueError(
            f"Working directory {workdir} is not under any allowed prefix: {self.allowed_workdirs}"
        )

    @staticmethod
    def _validate_command(command: str) -> None:
        """Reject commands matching known-dangerous patterns (C1 defense-in-depth)."""
        for pattern in _BLOCKED_COMMAND_PATTERNS:
            if pattern.search(command):
                raise ValueError(
                    f"Command rejected by safety filter: matches blocked pattern {pattern.pattern!r}"
                )

    @staticmethod
    def _validate_caller_env(env: dict[str, str]) -> None:
        """Reject caller-supplied env vars that could subvert process security (C3)."""
        for key in env:
            upper = key.upper()
            if upper in _BLOCKED_ENV_VARS:
                raise ValueError(f"Environment variable {key!r} is blocked for security reasons")
            for prefix in _BLOCKED_ENV_PREFIXES:
                if upper.startswith(prefix):
                    raise ValueError(
                        f"Environment variable {key!r} is blocked for security reasons"
                    )

    @staticmethod
    def _build_safe_env(caller_env: dict[str, str] | None) -> dict[str, str]:
        """Build a minimal process env from allowed host vars + caller overrides (C2)."""
        safe: dict[str, str] = {}
        for key, value in os.environ.items():
            if key in _SAFE_ENV_VARS or any(key.startswith(p) for p in _SAFE_ENV_PREFIXES):
                safe[key] = value
        if caller_env:
            safe.update(caller_env)
        return safe

    def _start_cleanup_thread(self, interval: int) -> threading.Thread:
        """Start a daemon thread that periodically calls cleanup_stale()."""

        def _cleanup_loop() -> None:
            while not self._cleanup_stop.wait(timeout=interval):
                try:
                    cleaned = self.cleanup_stale()
                    if cleaned:
                        logger.info("Periodic cleanup removed %d stale sessions", cleaned)
                except Exception:
                    logger.warning("Periodic cleanup failed", exc_info=True)

        thread = threading.Thread(target=_cleanup_loop, daemon=True)
        thread.start()
        return thread

    def _start_reader_thread(
        self,
        stream: Any,
        buffer: deque[str],
        combined: deque[str],
        prefix: str,
        session: ProcessSession,
    ) -> threading.Thread:
        """Start a daemon thread that drains a stream into ring buffers."""

        def _reader() -> None:
            try:
                for line in stream:
                    stripped = line.rstrip("\n")
                    buffer.append(stripped)
                    combined.append(f"[{prefix}] {stripped}")
                    with session._output_lock:
                        session.output_lines += 1
                        session.last_output_at = datetime.now(UTC)
            except (ValueError, OSError):
                # Stream closed
                pass

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        return thread

    def spawn(
        self,
        command: str,
        workdir: str | None = None,
        background: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Spawn a new process.

        Args:
            command: Shell command string to execute.
            workdir: Working directory. Validated for existence.
            background: If False, wait for completion and return output.
            timeout: Timeout in seconds (foreground) or max lifetime (background).
            env: Additional environment variables.

        Returns:
            Dict with session info and output (for foreground commands).
        """
        if not command or not command.strip():
            raise ValueError("Command must not be empty")

        # C1: reject known-dangerous commands
        self._validate_command(command)

        # C3: reject dangerous caller-supplied env vars
        if env:
            self._validate_caller_env(env)

        # Validate workdir
        if workdir:
            if not os.path.isabs(workdir):
                raise ValueError("Working directory must be an absolute path")
            if not os.path.isdir(workdir):
                raise ValueError(f"Working directory does not exist: {workdir}")
            self._validate_workdir(workdir)

        # Check session limit — reserve a slot for background spawns (TOCTOU fix)
        with self._lock:
            active = sum(1 for s in self._sessions.values() if s.status == "running")
            if active + self._pending_spawns >= self.max_sessions:
                raise RuntimeError(
                    f"Maximum concurrent sessions ({self.max_sessions}) reached. "
                    "Kill an existing session first."
                )
            if background:
                self._pending_spawns += 1

        # C2: build minimal environment instead of inheriting everything
        proc_env = self._build_safe_env(env)

        session_id = self._generate_session_id(command)

        if not background:
            return self._run_foreground(command, workdir, timeout, proc_env, session_id)

        try:
            return self._run_background(command, workdir, proc_env, session_id, timeout)
        except Exception:
            with self._lock:
                self._pending_spawns -= 1
            raise

    def _run_foreground(
        self,
        command: str,
        workdir: str | None,
        timeout: int,
        env: dict[str, str],
        session_id: str,
    ) -> dict[str, Any]:
        """Run a command in the foreground, blocking until completion."""
        try:
            result = subprocess.run(
                ["bash", "-c", command],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return {
                "session_id": session_id,
                "command": command,
                "background": False,
                "status": "exited",
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.TimeoutExpired as e:
            return {
                "session_id": session_id,
                "command": command,
                "background": False,
                "status": "timeout",
                "exit_code": -1,
                "stdout": e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(),
                "stderr": e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode(),
                "error": f"Command timed out after {timeout}s",
            }

    def _run_background(
        self,
        command: str,
        workdir: str | None,
        env: dict[str, str],
        session_id: str,
        timeout: int,
    ) -> dict[str, Any]:
        """Spawn a background process with output buffering."""
        process = subprocess.Popen(
            ["bash", "-c", command],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            env=env,
        )

        session = ProcessSession(
            session_id=session_id,
            pid=process.pid,
            command=command,
            workdir=workdir,
            process=process,
            stdout_buffer=deque(maxlen=self.buffer_lines),
            stderr_buffer=deque(maxlen=self.buffer_lines),
            combined_buffer=deque(maxlen=self.buffer_lines),
        )

        # Start reader threads
        self._start_reader_thread(
            process.stdout, session.stdout_buffer, session.combined_buffer, "out", session
        )
        self._start_reader_thread(
            process.stderr, session.stderr_buffer, session.combined_buffer, "err", session
        )

        # Start a watchdog thread for timeout
        if timeout > 0:
            self._start_timeout_watchdog(session, timeout)

        with self._lock:
            self._sessions[session_id] = session
            self._pending_spawns -= 1

        logger.info(
            "Spawned background process: %s (pid=%d, session=%s)",
            command,
            process.pid,
            session_id,
        )

        return {
            "session_id": session_id,
            "pid": process.pid,
            "command": command,
            "background": True,
            "status": "running",
        }

    def _start_timeout_watchdog(self, session: ProcessSession, timeout: int) -> None:
        """Start a daemon thread that kills the process after timeout."""

        def _watchdog() -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if session.process.poll() is not None:
                    return  # Process already exited
                time.sleep(1)
            # Timeout reached - kill
            if session.process.poll() is None:
                logger.warning(
                    "Process %s (pid=%d) timed out after %ds, killing",
                    session.session_id,
                    session.pid,
                    timeout,
                )
                try:
                    session.process.kill()
                    session.status = "timeout"
                except OSError:
                    pass

        thread = threading.Thread(target=_watchdog, daemon=True)
        thread.start()

    def _refresh_status(self, session: ProcessSession) -> None:
        """Update session status from process state."""
        if session.status in ("killed", "timeout"):
            return
        rc = session.process.poll()
        if rc is not None:
            session.status = "exited"
            session.exit_code = rc

    def poll(self, session_id: str) -> dict[str, Any]:
        """Get the current status of a session."""
        session = self._get_session(session_id)
        self._refresh_status(session)
        return session.to_dict()

    def log(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        """Get combined output lines from a session.

        Args:
            session_id: Session identifier.
            limit: Max lines to return.
            offset: Number of lines to skip from the start of the buffer.

        Returns:
            List of prefixed log lines.
        """
        session = self._get_session(session_id)
        self._refresh_status(session)
        lines = list(session.combined_buffer)
        return lines[offset : offset + limit]

    def write(self, session_id: str, data: str) -> dict[str, Any]:
        """Write data to a process's stdin.

        Args:
            session_id: Session identifier.
            data: Text to write (newline appended if missing).

        Returns:
            Status dict.
        """
        session = self._get_session(session_id)
        self._refresh_status(session)

        if session.status != "running":
            raise RuntimeError(f"Session {session_id} is not running (status={session.status})")

        if session.process.stdin is None:
            raise RuntimeError(f"Session {session_id} has no stdin pipe")

        if not data.endswith("\n"):
            data += "\n"

        try:
            session.process.stdin.write(data)
            session.process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"Failed to write to session {session_id}: {e}") from e

        return {"session_id": session_id, "written": len(data)}

    _ALLOWED_SIGNALS = frozenset({signal.SIGTERM, signal.SIGKILL})

    def kill(self, session_id: str, sig: int = signal.SIGTERM) -> dict[str, Any]:
        """Kill a process by session ID.

        Args:
            session_id: Session identifier.
            sig: Signal to send (default SIGTERM). Only SIGTERM and SIGKILL are allowed.

        Returns:
            Status dict.
        """
        if sig not in self._ALLOWED_SIGNALS:
            raise ValueError(
                f"Signal {sig} not allowed. Use SIGTERM ({signal.SIGTERM}) "
                f"or SIGKILL ({signal.SIGKILL})."
            )

        session = self._get_session(session_id)
        self._refresh_status(session)

        if session.status != "running":
            return {
                "session_id": session_id,
                "status": session.status,
                "message": "Process already stopped",
            }

        try:
            session.process.send_signal(sig)
            # Give it a moment to exit
            try:
                session.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.process.kill()
            session.status = "killed"
            session.exit_code = session.process.returncode
        except OSError as e:
            logger.warning("Failed to kill session %s: %s", session_id, e)
            raise RuntimeError(f"Failed to kill session {session_id}: {e}") from e

        logger.info("Killed session %s (pid=%d)", session_id, session.pid)
        return {
            "session_id": session_id,
            "status": "killed",
            "exit_code": session.exit_code,
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions with their current status."""
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            self._refresh_status(s)
        return [s.to_dict() for s in sessions]

    def cleanup_stale(self, max_age_hours: int | None = None) -> int:
        """Kill and remove sessions older than max_age_hours.

        Returns:
            Number of sessions cleaned up.
        """
        max_age = max_age_hours if max_age_hours is not None else self.max_age_hours
        cutoff = datetime.now(UTC).timestamp() - (max_age * 3600)
        cleaned = 0

        with self._lock:
            stale_ids = [
                sid
                for sid, session in self._sessions.items()
                if session.started_at.timestamp() < cutoff
            ]

        for sid in stale_ids:
            try:
                session = self._sessions.get(sid)
                if session and session.process.poll() is None:
                    session.process.kill()
                    session.process.wait(timeout=5)
                with self._lock:
                    self._sessions.pop(sid, None)
                cleaned += 1
                logger.info("Cleaned up stale session %s", sid)
            except Exception:
                logger.warning("Failed to clean up session %s", sid, exc_info=True)

        return cleaned

    def _get_session(self, session_id: str) -> ProcessSession:
        """Get a session by ID, raising if not found."""
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id}")
        return session

    def shutdown(self) -> None:
        """Kill all running sessions. Called during server shutdown."""
        self._cleanup_stop.set()
        if self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)

        with self._lock:
            session_ids = list(self._sessions.keys())

        for sid in session_ids:
            try:
                session = self._sessions.get(sid)
                if session and session.process.poll() is None:
                    session.process.kill()
                    session.process.wait(timeout=5)
            except Exception:
                logger.warning("Failed to kill session %s during shutdown", sid, exc_info=True)

        with self._lock:
            self._sessions.clear()

        logger.info("ProcessManager shut down, killed %d sessions", len(session_ids))
