"""Host-side session manager for interactive PTY containers.

Each ``start`` action spins up a dedicated Docker container running
``pty_runner.py``; subsequent actions (send_input, read_output, etc.)
route to the container by session_id; ``close`` tears it down.

Thread-safe singleton — avoids changing the ``execute_tool_call`` signature.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid

from creel.container_pool import ManagedContainer
from creel.containers import _ensure_image
from creel.models import ExecutorConfig, ToolConfig

logger = logging.getLogger(__name__)

_IDLE_CHECK_INTERVAL = 30  # seconds
_SESSION_TIMEOUT = 600  # seconds — reap sessions idle longer than this


class InteractiveSessionManager:
    """Maps session_id -> ManagedContainer for interactive PTY sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, ManagedContainer] = {}
        self._session_started: dict[str, float] = {}
        self._closed = False
        self._reaper: threading.Timer | None = None
        self._start_idle_reaper()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        executor_config: ExecutorConfig,
        tool_config: ToolConfig,
    ) -> str:
        """Route an exec_interactive action to the right container.

        Returns the executor result as a JSON string.
        """
        action = executor_config.args.get("action", "")

        if action == "start":
            return self._start(executor_config, tool_config)
        if action == "list_sessions":
            return self._list_sessions()
        # Everything else routes to an existing session
        return self._forward(executor_config, action)

    # ------------------------------------------------------------------
    # Internal: start / forward / cleanup
    # ------------------------------------------------------------------

    def _start(
        self,
        executor_config: ExecutorConfig,
        tool_config: ToolConfig,
    ) -> str:
        """Spin up a new container, send the start message, register the session."""
        command = executor_config.args.get("command", "")
        if not command:
            return json.dumps({"success": False, "error": "No command provided"})

        timeout = int(executor_config.args.get("timeout", "300"))
        cols = int(executor_config.args.get("cols", "120"))
        rows = int(executor_config.args.get("rows", "40"))

        # Build the Docker image
        raw_image = tool_config.image if tool_config.image else executor_config.image
        image = _ensure_image(raw_image)

        container_id = uuid.uuid4().hex[:12]
        container_name = f"creel-pty-{container_id}"

        # Security flags matching Creel conventions
        writable = tool_config.writable
        memory = tool_config.memory or "256m"
        cpus = tool_config.cpus or "0.5"
        tmpfs_size = tool_config.tmpfs_size or "16M"

        docker_flags: list[str] = []
        if not writable:
            docker_flags.append("--read-only")
        docker_flags.extend(
            [
                "--tmpfs",
                f"/tmp:rw,noexec,nosuid,size={tmpfs_size}",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                f"--memory={memory}",
                f"--cpus={cpus}",
            ]
        )
        if not tool_config.network:
            docker_flags.append("--network=none")

        # Write an empty env file (no secrets needed for PTY)
        env_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".env",
            delete=False,
            prefix="creel-pty-",
        )
        env_file.close()
        env_file_path = env_file.name

        docker_cmd = [
            "docker",
            "run",
            "-i",
            "--name",
            container_name,
            "--env-file",
            env_file_path,
            *docker_flags,
            image,
            "python",
            "pty_runner.py",
        ]

        logger.info("Starting interactive container %s for command %r", container_name, command)

        proc = subprocess.Popen(
            docker_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        container = ManagedContainer(
            id=container_id,
            image=image,
            entrypoint="python pty_runner.py",
            proc=proc,
            env_file_path=env_file_path,
            container_name=container_name,
        )

        # Health-check
        if not container.ping():
            container.force_kill()
            self._docker_rm(container_name)
            return json.dumps({"success": False, "error": "Container failed health check"})

        # Send start message
        container.send(
            {
                "type": "start",
                "command": command,
                "timeout": timeout,
                "cols": cols,
                "rows": rows,
            }
        )

        response_timeout = float(timeout + 10)
        try:
            msg = container.recv(timeout=response_timeout)
        except Exception as e:
            container.force_kill()
            self._docker_rm(container_name)
            return json.dumps({"success": False, "error": f"Container start failed: {e}"})

        if not msg.get("success"):
            container.force_kill()
            self._docker_rm(container_name)
            return json.dumps(msg)

        session_id = msg.get("session_id", "")

        with self._lock:
            self._sessions[session_id] = container
            self._session_started[session_id] = time.monotonic()

        logger.info("Interactive session %s started in container %s", session_id, container_name)

        # Return the response without the protocol "type" field
        result = {k: v for k, v in msg.items() if k != "type"}
        return json.dumps(result, indent=2)

    def _forward(self, executor_config: ExecutorConfig, action: str) -> str:
        """Forward a message to an existing session's container."""
        session_id = executor_config.args.get("session_id", "")
        if not session_id:
            return json.dumps({"success": False, "error": f"'{action}' requires 'session_id'"})

        with self._lock:
            container = self._sessions.get(session_id)

        if container is None:
            return json.dumps({"success": False, "error": f"Session not found: {session_id}"})

        if not container.alive:
            self._cleanup_session(session_id)
            return json.dumps({"success": False, "error": f"Session {session_id} container died"})

        # Build the message from executor args
        msg: dict = {"type": action, "session_id": session_id}

        if action == "send_input":
            msg["input"] = executor_config.args.get("input", "")
        elif action == "read_output":
            msg["read_timeout"] = executor_config.args.get("read_timeout", "10")
        elif action == "resize":
            msg["cols"] = executor_config.args.get("cols", "120")
            msg["rows"] = executor_config.args.get("rows", "40")

        container.send(msg)

        # Use a generous timeout — the container handles its own timeouts
        recv_timeout = float(executor_config.timeout or 300) + 10
        try:
            response = container.recv(timeout=recv_timeout)
        except Exception as e:
            self._cleanup_session(session_id)
            return json.dumps({"success": False, "error": f"Container communication failed: {e}"})

        # Clean up after close
        if action == "close":
            self._cleanup_session(session_id)

        result = {k: v for k, v in response.items() if k != "type"}
        return json.dumps(result, indent=2)

    def _list_sessions(self) -> str:
        """List all active sessions managed by this instance."""
        with self._lock:
            sessions = []
            for sid, container in self._sessions.items():
                started = self._session_started.get(sid, 0)
                sessions.append(
                    {
                        "session_id": sid,
                        "container": container.container_name,
                        "alive": container.alive,
                        "elapsed": round(time.monotonic() - started, 1),
                    }
                )
        return json.dumps({"success": True, "sessions": sessions}, indent=2)

    def _cleanup_session(self, session_id: str) -> None:
        """Shut down a session's container and remove it from tracking."""
        with self._lock:
            container = self._sessions.pop(session_id, None)
            self._session_started.pop(session_id, None)

        if container is None:
            return

        logger.info("Cleaning up session %s (container %s)", session_id, container.container_name)
        container.shutdown()
        self._docker_rm(container.container_name)

        # Clean up env file
        try:
            os.unlink(container.env_file_path)
        except Exception:
            pass

    def _docker_rm(self, container_name: str) -> None:
        """Force-remove a Docker container by name."""
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            logger.warning("Failed to remove container %s", container_name)

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    def _start_idle_reaper(self) -> None:
        """Periodically clean up sessions whose containers have died."""
        if self._closed:
            return

        def _reap() -> None:
            if self._closed:
                return
            now = time.monotonic()
            to_remove: list[str] = []

            with self._lock:
                for sid, container in self._sessions.items():
                    started = self._session_started.get(sid, now)
                    if not container.alive or (now - started) > _SESSION_TIMEOUT:
                        to_remove.append(sid)

            for sid in to_remove:
                logger.info("Reaping dead/expired interactive session %s", sid)
                self._cleanup_session(sid)

            self._start_idle_reaper()

        self._reaper = threading.Timer(_IDLE_CHECK_INTERVAL, _reap)
        self._reaper.daemon = True
        self._reaper.start()

    def shutdown(self) -> None:
        """Shut down all sessions and stop the reaper."""
        self._closed = True
        if self._reaper:
            self._reaper.cancel()

        with self._lock:
            session_ids = list(self._sessions.keys())

        for sid in session_ids:
            self._cleanup_session(sid)

        logger.info("InteractiveSessionManager shut down (%d sessions)", len(session_ids))


# Module-level singleton
_manager: InteractiveSessionManager | None = None
_manager_lock = threading.Lock()


def get_session_manager() -> InteractiveSessionManager:
    """Get or create the singleton InteractiveSessionManager."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = InteractiveSessionManager()
    return _manager
