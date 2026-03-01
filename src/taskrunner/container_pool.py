"""Warm container pool for LLM containers.

Keeps Docker containers alive between requests to eliminate cold-start
latency.  Containers communicate via the existing JSON-over-stdio protocol
with added ping/reset/shutdown lifecycle messages.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# How long to wait for a pong before declaring the container dead.
_PING_TIMEOUT = 5.0  # seconds
_DEFAULT_IDLE_TIMEOUT = 300  # 5 minutes
_DEFAULT_MAX_CONTAINERS = 2


@dataclass
class ContainerPoolConfig:
    """Configuration for the warm container pool."""

    enabled: bool = True
    idle_timeout_seconds: int = _DEFAULT_IDLE_TIMEOUT
    max_containers: int = _DEFAULT_MAX_CONTAINERS


@dataclass
class ManagedContainer:
    """A long-lived container process managed by the pool."""

    id: str
    image: str
    entrypoint: str
    proc: subprocess.Popen
    env_file_path: str
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def alive(self) -> bool:
        """Check if the underlying process is still running."""
        return self.proc.poll() is None

    def send(self, msg: dict) -> None:
        """Write a JSON line to the container's stdin."""
        with self._lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def recv(self, timeout: float | None = None) -> dict:
        """Read a JSON line from the container's stdout.

        Args:
            timeout: Max seconds to wait. None = block forever.

        Raises:
            RuntimeError: If the container exited or timed out.
        """
        if timeout is not None:
            import select

            ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
            if not ready:
                raise TimeoutError(f"Container {self.id} did not respond within {timeout}s")

        line = self.proc.stdout.readline()
        if not line:
            retcode = self.proc.poll()
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(
                f"Container {self.id} exited unexpectedly (code={retcode}). stderr: {stderr[:500]}"
            )
        return json.loads(line)

    def ping(self) -> bool:
        """Send a ping and wait for pong. Returns True if healthy."""
        try:
            self.send({"type": "ping"})
            msg = self.recv(timeout=_PING_TIMEOUT)
            return msg.get("type") == "pong"
        except Exception:
            logger.debug("Ping failed for container %s", self.id)
            return False

    def reset(self) -> bool:
        """Ask the container to clear state for next session.

        Returns True if the container acknowledged the reset.
        """
        try:
            self.send({"type": "reset"})
            msg = self.recv(timeout=_PING_TIMEOUT)
            return msg.get("type") == "ready"
        except Exception:
            logger.debug("Reset failed for container %s", self.id)
            return False

    def shutdown(self) -> None:
        """Send a graceful shutdown message, then wait for exit."""
        try:
            if self.alive:
                self.send({"type": "shutdown"})
                self.proc.wait(timeout=5)
        except Exception:
            logger.debug("Graceful shutdown failed for %s, killing", self.id)
            self.force_kill()

    def force_kill(self) -> None:
        """Force-kill the container process."""
        try:
            if self.alive:
                self.proc.kill()
                self.proc.wait(timeout=5)
        except Exception:
            logger.warning("Failed to kill container %s", self.id)


class ContainerPool:
    """Pool of warm LLM containers keyed by (image, entrypoint).

    Thread-safe. Containers are reused between requests to avoid
    Docker cold-start latency.
    """

    def __init__(self, config: ContainerPoolConfig | None = None):
        self._config = config or ContainerPoolConfig()
        self._lock = threading.RLock()
        # Key: (image, entrypoint) -> list of idle ManagedContainers
        self._idle: dict[tuple[str, str], list[ManagedContainer]] = {}
        self._all: list[ManagedContainer] = []
        self._idle_timer: threading.Timer | None = None
        self._closed = False

        if self._config.enabled:
            self._start_idle_reaper()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def acquire(
        self,
        image: str,
        entrypoint: str,
        docker_flags: list[str],
        env_vars: dict[str, str],
    ) -> ManagedContainer:
        """Get a warm container or start a new one.

        Args:
            image: Docker image to run.
            entrypoint: The script to run (e.g. "agent_runner.py").
            docker_flags: Security/resource flags for docker run.
            env_vars: Environment variables to pass to the container.

        Returns:
            A ManagedContainer ready for use.
        """
        key = (image, entrypoint)

        # Pop all idle candidates under the lock, then health-check outside
        # to avoid blocking the pool during slow pings.
        with self._lock:
            candidates = list(self._idle.get(key, []))
            if key in self._idle:
                self._idle[key].clear()

        for i, container in enumerate(candidates):
            if container.alive and container.ping():
                container.last_used = time.monotonic()
                # Return remaining candidates to idle under lock
                remaining = candidates[i + 1 :]
                if remaining:
                    with self._lock:
                        self._idle.setdefault(key, []).extend(remaining)
                logger.info(
                    "Reusing warm container %s (%s/%s)",
                    container.id,
                    image,
                    entrypoint,
                )
                return container
            else:
                logger.debug("Discarding dead idle container %s", container.id)
                with self._lock:
                    self._remove_container(container)

        # No reusable container — start a fresh one
        return self._start_container(image, entrypoint, docker_flags, env_vars)

    def release(self, container: ManagedContainer) -> None:
        """Return a container to the pool for reuse.

        The container's state is reset before being made available.
        If reset fails, the container is discarded.
        """
        if self._closed:
            container.shutdown()
            return

        if not container.alive:
            logger.debug("Cannot release dead container %s", container.id)
            self._cleanup_container(container)
            return

        # Reset container state for next session
        if not container.reset():
            logger.info("Container %s failed reset, discarding", container.id)
            self._cleanup_container(container)
            return

        container.last_used = time.monotonic()
        key = (container.image, container.entrypoint)

        with self._lock:
            idle_list = self._idle.setdefault(key, [])

            # Enforce max pool size
            while len(idle_list) >= self._config.max_containers:
                evicted = idle_list.pop(0)  # evict oldest
                logger.info("Evicting container %s (pool full)", evicted.id)
                self._remove_container(evicted)

            idle_list.append(container)
            logger.info(
                "Released container %s back to pool (%d idle for %s/%s)",
                container.id,
                len(idle_list),
                *key,
            )

    def shutdown(self) -> None:
        """Shut down all containers and stop the idle reaper."""
        self._closed = True

        if self._idle_timer:
            self._idle_timer.cancel()

        with self._lock:
            containers = list(self._all)
            self._all.clear()
            self._idle.clear()

        for container in containers:
            logger.debug("Shutting down container %s", container.id)
            container.shutdown()

        logger.info("Container pool shut down (%d containers)", len(containers))

    def stats(self) -> dict:
        """Return pool statistics."""
        with self._lock:
            total_idle = sum(len(v) for v in self._idle.values())
            return {
                "total_containers": len(self._all),
                "idle_containers": total_idle,
                "pool_keys": list(self._idle.keys()),
                "enabled": self._config.enabled,
            }

    def _start_container(
        self,
        image: str,
        entrypoint: str,
        docker_flags: list[str],
        env_vars: dict[str, str],
    ) -> ManagedContainer:
        """Start a new Docker container."""
        container_id = uuid.uuid4().hex[:12]
        container_name = f"creel-llm-{container_id}"

        # Write env file
        env_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".env",
            delete=False,
            prefix="creel-pool-",
        )
        for key, value in env_vars.items():
            sanitized = value.replace("\n", "").replace("\r", "")
            env_file.write(f"{key}={sanitized}\n")
        env_file.flush()
        env_file_path = env_file.name
        env_file.close()

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
            entrypoint,
        ]

        logger.info(
            "Starting warm container %s (%s/%s)",
            container_id,
            image,
            entrypoint,
        )

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
            entrypoint=entrypoint,
            proc=proc,
            env_file_path=env_file_path,
        )

        with self._lock:
            self._all.append(container)

        return container

    def _cleanup_container(self, container: ManagedContainer) -> None:
        """Shut down and remove a container from tracking."""
        container.shutdown()
        with self._lock:
            self._remove_container(container)

    def _remove_container(self, container: ManagedContainer) -> None:
        """Remove a container from internal tracking (caller holds lock)."""
        try:
            self._all.remove(container)
        except ValueError:
            pass

        # Clean up the docker container (it may still exist without --rm)
        try:
            subprocess.run(
                ["docker", "rm", "-f", f"creel-llm-{container.id}"],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            logger.warning(
                "Failed to remove Docker container creel-llm-%s; it may be orphaned",
                container.id,
            )

        # Clean up env file
        try:
            import os

            os.unlink(container.env_file_path)
        except Exception:
            pass

    def _start_idle_reaper(self) -> None:
        """Periodically evict containers that have been idle too long."""
        if self._closed:
            return

        timeout = self._config.idle_timeout_seconds

        def _reap() -> None:
            if self._closed:
                return

            now = time.monotonic()
            to_remove: list[ManagedContainer] = []

            with self._lock:
                for key, idle_list in list(self._idle.items()):
                    expired = [c for c in idle_list if (now - c.last_used) > timeout or not c.alive]
                    for c in expired:
                        idle_list.remove(c)
                        to_remove.append(c)
                    if not idle_list:
                        del self._idle[key]

            for c in to_remove:
                logger.info(
                    "Evicting idle container %s (idle %.0fs)",
                    c.id,
                    now - c.last_used,
                )
                c.shutdown()
                with self._lock:
                    self._remove_container(c)

            # Reschedule
            self._start_idle_reaper()

        # Check every 30 seconds (or half the idle timeout, whichever is less)
        interval = min(30, timeout / 2)
        self._idle_timer = threading.Timer(interval, _reap)
        self._idle_timer.daemon = True
        self._idle_timer.start()
