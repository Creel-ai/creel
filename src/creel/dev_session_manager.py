"""Host-side session manager for containerized dev sessions.

Manages a single long-lived Docker container running ``dev_session_runner.py``
with an in-container ProcessManager.  Tool calls (dev_exec, dev_process,
dev_sessions) are serialized as JSON-over-stdio messages to the container.

The container is started lazily on the first tool call and kept alive for
the duration of the conversation.  An idle reaper cleans it up if left
unattended.

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
from typing import Any

from creel.container_pool import ManagedContainer
from creel.containers import _ensure_image
from creel.models import ExecutorConfig, ToolConfig

logger = logging.getLogger(__name__)

_IDLE_CHECK_INTERVAL = 60  # seconds
_CONTAINER_TIMEOUT = 14400  # 4 hours — max container lifetime


def _safe_int(value: str, default: int) -> int:
    """Parse an int from a string, returning *default* on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


class DevSessionManager:
    """Manages a single long-lived dev session container.

    Security notes:
    - The container is intentionally writable (no ``--read-only``) because
      dev sessions need to install packages and write files.  All other
      hardening flags (cap-drop, no-new-privileges, resource limits,
      pids-limit) are applied.
    - Network is enabled by default so dev tools (npm, pip) can fetch
      dependencies.  Cloud metadata endpoints are blocked to prevent
      SSRF against instance metadata services.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._container: ManagedContainer | None = None
        self._container_name: str = ""
        self._env_file_path: str = ""
        self._started_at: float = 0.0
        self._closed = False
        self._reaper_stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None
        self._start_idle_reaper()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        executor_config: ExecutorConfig,
        tool_config: ToolConfig,
    ) -> str:
        """Route a dev_session tool call to the container.

        Returns the executor result as a JSON string.
        """
        container = self._ensure_container(tool_config)
        action = executor_config.args.get("_action", "")

        if action == "exec":
            return self._exec(container, executor_config)
        if action == "process":
            return self._process(container, executor_config)
        if action == "sessions":
            return self._sessions(container)

        return json.dumps({"error": f"Unknown dev_session action: {action}"})

    # ------------------------------------------------------------------
    # Internal: container lifecycle
    # ------------------------------------------------------------------

    def _ensure_container(self, tool_config: ToolConfig) -> ManagedContainer:
        """Start the container lazily on first call, or restart if dead.

        The lock is held across both the liveness check and the start to
        prevent a TOCTOU race where two threads both see a dead container
        and start competing replacements.
        """
        with self._lock:
            if self._container is not None and self._container.alive:
                return self._container
            # Still under lock — start (or restart) the container.
            return self._start_container_locked(tool_config)

    def _start_container_locked(self, tool_config: ToolConfig) -> ManagedContainer:
        """Spin up the Docker container.  Caller must hold ``_lock``."""
        if self._closed:
            raise RuntimeError("DevSessionManager has been shut down")

        # Clean up any previous container (release lock briefly for cleanup
        # I/O, then re-acquire — safe because _ensure_container holds the
        # lock and is the only caller).
        old_container = self._container
        old_name = self._container_name
        old_env = self._env_file_path
        self._container = None
        self._container_name = ""
        self._env_file_path = ""

        if old_container is not None:
            # Release lock for potentially slow Docker cleanup, then
            # re-acquire.  No other thread can enter _start because
            # _ensure_container holds the outer lock.
            self._lock.release()
            try:
                logger.info("Cleaning up previous dev session container %s", old_name)
                old_container.shutdown()
                self._docker_rm(old_name)
                self._unlink(old_env)
            finally:
                self._lock.acquire()

        # Build/ensure the Docker image
        raw_image = tool_config.image or "executor-dev-session:latest"
        image = _ensure_image(raw_image)

        container_id = uuid.uuid4().hex[:12]
        container_name = f"creel-devsession-{container_id}"

        # Security and resource flags
        memory = tool_config.memory or "512m"
        cpus = tool_config.cpus or "1.0"
        tmpfs_size = tool_config.tmpfs_size or "256M"

        docker_flags: list[str] = [
            # Dev containers are intentionally writable (no --read-only)
            # because they need to install packages and write files.
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={tmpfs_size}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--memory={memory}",
            f"--cpus={cpus}",
            # Limit PIDs to prevent fork bombs (defense-in-depth beyond
            # the ProcessManager regex blocklist).
            "--pids-limit=256",
            # Block cloud metadata endpoints to prevent SSRF.
            "--add-host=metadata.google.internal:127.0.0.1",
            "--add-host=169.254.169.254:127.0.0.1",
        ]
        if not tool_config.network:
            docker_flags.append("--network=none")

        # Volume mounts from tool_config
        if tool_config.mounts:
            for mount in tool_config.mounts:
                host_path = os.path.expanduser(mount.path)
                host_path = os.path.realpath(host_path)
                container_path = f"/mnt{host_path}"
                mode = mount.mode or "ro"
                docker_flags.extend(["-v", f"{host_path}:{container_path}:{mode}"])

        # Write an empty env file (no secrets needed for dev session)
        env_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".env",
            delete=False,
            prefix="creel-devsession-",
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
        ]

        logger.info("Starting dev session container %s (image=%s)", container_name, image)

        try:
            proc = subprocess.Popen(
                docker_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception:
            self._unlink(env_file_path)
            raise

        container = ManagedContainer(
            id=container_id,
            image=image,
            entrypoint="python /app/dev_session_runner.py",
            proc=proc,
            env_file_path=env_file_path,
            container_name=container_name,
        )

        # Health-check
        if not container.ping():
            container.force_kill()
            self._docker_rm(container_name)
            self._unlink(env_file_path)
            raise RuntimeError("Dev session container failed health check")

        self._container = container
        self._container_name = container_name
        self._env_file_path = env_file_path
        self._started_at = time.monotonic()

        logger.info("Dev session container %s ready", container_name)
        return container

    # ------------------------------------------------------------------
    # Internal: tool dispatch
    # ------------------------------------------------------------------

    def _mark_unhealthy(self) -> None:
        """Mark the container as unhealthy so the next call restarts it."""
        with self._lock:
            self._container = None

    def _exec(self, container: ManagedContainer, config: ExecutorConfig) -> str:
        """Handle a dev_exec tool call."""
        command = config.args.get("command", "")
        if not command:
            return json.dumps({"error": "dev_exec requires a 'command' argument"})

        background = config.args.get("background", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        workdir = config.args.get("workdir")
        timeout = _safe_int(config.args.get("timeout", "300"), 300)

        msg: dict[str, Any] = {
            "type": "exec",
            "command": command,
            "background": background,
            "timeout": timeout,
        }
        if workdir:
            msg["workdir"] = workdir

        container.send(msg)

        # For foreground commands, use a generous recv timeout
        recv_timeout = float(timeout + 30) if not background else 30.0
        try:
            response = container.recv(timeout=recv_timeout)
        except Exception as e:
            self._mark_unhealthy()
            return json.dumps({"error": f"Container communication failed: {e}"})

        return json.dumps(response, indent=2)

    def _process(self, container: ManagedContainer, config: ExecutorConfig) -> str:
        """Handle a dev_process tool call."""
        session_id = config.args.get("session_id", "")
        if not session_id:
            return json.dumps({"error": "dev_process requires a 'session_id' argument"})

        action = config.args.get("action", "poll")
        limit = _safe_int(config.args.get("limit", "100"), 100)
        offset = _safe_int(config.args.get("offset", "0"), 0)

        msg: dict[str, Any] = {
            "type": "process",
            "session_id": session_id,
            "action": action,
            "limit": limit,
            "offset": offset,
        }
        data = config.args.get("data")
        if data is not None:
            msg["data"] = data

        container.send(msg)

        try:
            response = container.recv(timeout=60.0)
        except Exception as e:
            self._mark_unhealthy()
            return json.dumps({"error": f"Container communication failed: {e}"})

        return json.dumps(response, indent=2)

    def _sessions(self, container: ManagedContainer) -> str:
        """Handle a dev_sessions tool call."""
        container.send({"type": "sessions"})

        try:
            response = container.recv(timeout=10.0)
        except Exception as e:
            self._mark_unhealthy()
            return json.dumps({"error": f"Container communication failed: {e}"})

        return json.dumps(response, indent=2)

    # ------------------------------------------------------------------
    # Internal: cleanup
    # ------------------------------------------------------------------

    def _cleanup_container(self) -> None:
        """Shut down the current container if one exists."""
        with self._lock:
            container = self._container
            container_name = self._container_name
            env_file_path = self._env_file_path
            self._container = None
            self._container_name = ""
            self._env_file_path = ""

        if container is not None:
            logger.info("Cleaning up dev session container %s", container_name)
            container.shutdown()
            self._docker_rm(container_name)
            self._unlink(env_file_path)

    @staticmethod
    def _docker_rm(container_name: str) -> None:
        """Force-remove a Docker container by name."""
        if not container_name:
            return
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            logger.warning("Failed to remove container %s", container_name)

    @staticmethod
    def _unlink(path: str) -> None:
        """Remove a file, ignoring errors."""
        if not path:
            return
        try:
            os.unlink(path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Idle reaper (Event-based loop, matching ProcessManager pattern)
    # ------------------------------------------------------------------

    def _start_idle_reaper(self) -> None:
        """Start a daemon thread that periodically checks container health."""

        def _reaper_loop() -> None:
            while not self._reaper_stop.wait(timeout=_IDLE_CHECK_INTERVAL):
                try:
                    with self._lock:
                        container = self._container
                        started = self._started_at

                    if container is not None:
                        elapsed = time.monotonic() - started
                        if not container.alive or elapsed > _CONTAINER_TIMEOUT:
                            logger.info(
                                "Reaping dev session container (alive=%s, elapsed=%.0fs)",
                                container.alive,
                                elapsed,
                            )
                            self._cleanup_container()
                except Exception:
                    logger.warning("Dev session reaper error", exc_info=True)

        self._reaper_thread = threading.Thread(target=_reaper_loop, daemon=True)
        self._reaper_thread.start()

    def shutdown(self) -> None:
        """Tear down the container and stop the reaper."""
        self._closed = True
        self._reaper_stop.set()
        if self._reaper_thread and self._reaper_thread.is_alive():
            self._reaper_thread.join(timeout=5)
        self._cleanup_container()
        logger.info("DevSessionManager shut down")


# Module-level singleton
_manager: DevSessionManager | None = None
_manager_lock = threading.Lock()


def get_dev_session_manager() -> DevSessionManager:
    """Get or create the singleton DevSessionManager."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = DevSessionManager()
    return _manager


def shutdown_dev_session_manager() -> None:
    """Shut down the singleton DevSessionManager if it exists."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
            _manager = None
