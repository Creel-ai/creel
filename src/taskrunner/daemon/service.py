"""Thread-safe service layer for daemon-mode agent runtime."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from taskrunner.channels.base import Channel
from taskrunner.chat import ChatServer
from taskrunner.models import AgentDefinition
from taskrunner.scheduler import start_scheduler
from taskrunner.session import Session

logger = logging.getLogger(__name__)


class DaemonService:
    """Owns long-lived agent runtime state behind a stable service API.

    This class is intentionally transport-agnostic (HTTP/socket/TUI) and exposes
    methods suitable for daemon API handlers and local clients.
    """

    def __init__(
        self,
        agent_def: AgentDefinition,
        use_containers: bool = False,
        server: ChatServer | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._agent_def = agent_def
        self._use_containers = use_containers
        self._server = server or ChatServer(agent_def, use_containers=use_containers)
        self._now_fn = now_fn
        self._started_at = self._now_fn()
        self._lock = threading.RLock()

        # Scheduler lifecycle state.
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_shutdown_event: threading.Event | None = None

        self._shutdown_done = False

        # Channel/plugin lifecycle state.
        self._channels: dict[str, Channel] = {}
        self._channel_threads: dict[str, threading.Thread] = {}
        self._channel_state: dict[str, dict[str, Any]] = {}

    # --- Message and session API ---

    def send_message(self, sender_id: str, text: str) -> str:
        """Route a message through the agent loop and return the response text."""
        with self._lock:
            return self._server.handle_message(sender_id, text)

    def stream_message(
        self,
        sender_id: str,
        text: str,
        session_id: str | None = None,
    ):
        """Yield daemon streaming events for a single request."""
        active_session_id: str | None = None
        try:
            if session_id:
                resumed = self.resume_session(sender_id, session_id)
                active_session_id = resumed["session_id"]
            else:
                # Ensure we have a stable active session id before streaming.
                active = self.get_active_session(sender_id)
                active_session_id = active["session_id"]

            yield {
                "type": "start",
                "sender_id": sender_id,
                "session_id": active_session_id,
                "payload": {},
            }
        except Exception as exc:
            yield {
                "type": "error",
                "sender_id": sender_id,
                "session_id": active_session_id,
                "payload": {"error": str(exc)},
            }
            return

        token_queue: queue.Queue[str | object] = queue.Queue()
        sentinel = object()
        response_text = ""
        error: Exception | None = None

        def on_delta(chunk: str) -> None:
            if chunk:
                token_queue.put(chunk)

        def _run_agent() -> None:
            nonlocal response_text, error
            try:
                with self._lock:
                    response_text = self._server.handle_message(
                        sender_id,
                        text,
                        on_text_delta=on_delta,
                    )
            except Exception as exc:
                error = exc
            finally:
                token_queue.put(sentinel)

        worker = threading.Thread(
            target=_run_agent,
            name="creel-daemon-stream",
            daemon=True,
        )
        worker.start()

        while True:
            item = token_queue.get()
            if item is sentinel:
                break
            yield {
                "type": "token",
                "sender_id": sender_id,
                "session_id": active_session_id,
                "payload": {"text": str(item)},
            }

        worker.join()
        active_session_id = self.get_active_session_id(sender_id) or active_session_id
        if error is not None:
            yield {
                "type": "error",
                "sender_id": sender_id,
                "session_id": active_session_id,
                "payload": {"error": str(error)},
            }
            return

        yield {
            "type": "final",
            "sender_id": sender_id,
            "session_id": active_session_id,
            "payload": {"text": response_text},
        }

    def list_sessions(self, sender_id: str) -> list[dict]:
        """List persisted sessions for a sender."""
        with self._lock:
            return self._server._session_mgr.list_sessions(sender_id)

    def new_session(self, sender_id: str) -> dict:
        """Create and activate a new session for the sender."""
        with self._lock:
            session = self._server._session_mgr.new_session(sender_id)
            return self._session_summary(session)

    def resume_session(self, sender_id: str, session_id: str) -> dict:
        """Resume an existing session for a sender."""
        with self._lock:
            session = self._server._session_mgr.resume_session(sender_id, session_id)
            return self._session_summary(session)

    def clear_session(self, sender_id: str) -> None:
        """Clear message history for the active session."""
        with self._lock:
            self._server._session_mgr.clear(sender_id)

    def get_active_session(self, sender_id: str) -> dict:
        """Get the active session summary for a sender, creating one if needed."""
        with self._lock:
            session = self._server._session_mgr.get_or_create(sender_id)
            return self._session_summary(session)

    def get_active_session_id(self, sender_id: str) -> str | None:
        """Return active session id for sender, if any."""
        with self._lock:
            return self._server._session_mgr.get_active_session_id(sender_id)

    def get_history(
        self,
        sender_id: str,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get recent message history for a sender/session."""
        if limit < 1:
            return []

        with self._lock:
            mgr = self._server._session_mgr
            if session_id:
                session = mgr.load_session(session_id)
                if session is None or session.sender_id != sender_id:
                    raise ValueError(f"Session {session_id} not found")
            else:
                session = mgr.get_or_create(sender_id)
            return list(session.messages[-limit:])

    # --- Scheduler lifecycle ---

    def start_scheduler(self, tasks_dir: str | Path = "tasks") -> bool:
        """Start the scheduler in a background thread.

        Returns:
            True if the scheduler was started, False if already running.
        """
        with self._lock:
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                return False

            shutdown_event = threading.Event()
            tasks_path = Path(tasks_dir)

            def _run() -> None:
                try:
                    start_scheduler(
                        tasks_dir=tasks_path,
                        use_containers=self._use_containers,
                        shutdown_event=shutdown_event,
                    )
                except Exception:
                    logger.exception("Scheduler crashed")

            thread = threading.Thread(
                target=_run,
                name="creel-daemon-scheduler",
                daemon=True,
            )
            self._scheduler_shutdown_event = shutdown_event
            self._scheduler_thread = thread
            thread.start()
            return True

    def stop_scheduler(self, timeout: float = 5.0) -> bool:
        """Stop the scheduler background thread gracefully."""
        with self._lock:
            thread = self._scheduler_thread
            shutdown_event = self._scheduler_shutdown_event

        if not thread or not thread.is_alive():
            return False

        if shutdown_event is not None:
            shutdown_event.set()

        thread.join(timeout=timeout)
        stopped = not thread.is_alive()

        if stopped:
            with self._lock:
                self._scheduler_thread = None
                self._scheduler_shutdown_event = None

        return stopped

    # --- Channel/plugin lifecycle ---

    def register_channel(self, name: str, channel: Channel) -> None:
        """Register a channel plugin instance by name."""
        with self._lock:
            self._channels[name] = channel
            self._set_channel_state(name, running=False, detail="registered")

    def start_channel(self, name: str) -> bool:
        """Start a registered channel listener in a background thread."""
        with self._lock:
            channel = self._channels.get(name)
            if channel is None:
                raise ValueError(f"Unknown channel: {name}")

            existing = self._channel_threads.get(name)
            if existing and existing.is_alive():
                return False

            def _run_channel() -> None:
                self._set_channel_state(name, running=True, detail="listening")
                try:
                    channel.listen(self.send_message)
                except Exception as exc:
                    logger.exception("Channel '%s' crashed", name)
                    self._set_channel_state(name, running=False, detail=f"error: {exc}")
                    return
                self._set_channel_state(name, running=False, detail="stopped")

            thread = threading.Thread(
                target=_run_channel,
                name=f"creel-channel-{name}",
                daemon=True,
            )
            self._channel_threads[name] = thread
            thread.start()
            return True

    def stop_channel(self, name: str, timeout: float = 5.0) -> bool:
        """Request a channel stop and wait for the listener thread to finish."""
        with self._lock:
            channel = self._channels.get(name)
            thread = self._channel_threads.get(name)

        if channel is None:
            raise ValueError(f"Unknown channel: {name}")
        if thread is None or not thread.is_alive():
            self._set_channel_state(name, running=False, detail="stopped")
            return False

        channel.stop()
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()

        if stopped:
            with self._lock:
                self._channel_threads.pop(name, None)
                self._set_channel_state(name, running=False, detail="stopped")

        return stopped

    def shutdown(self, timeout: float = 5.0) -> None:
        """Gracefully stop scheduler and all registered channels.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        self.stop_scheduler(timeout=timeout)

        channel_names = []
        with self._lock:
            channel_names = list(self._channels.keys())

        for name in channel_names:
            try:
                self.stop_channel(name, timeout=timeout)
            except Exception:
                logger.exception("Failed to stop channel '%s'", name)

    # --- Status ---

    def status(self) -> dict[str, Any]:
        """Return daemon runtime status for status/health endpoints."""
        with self._lock:
            mgr = self._server._session_mgr
            stats = mgr.session_stats()

            scheduler_running = bool(
                self._scheduler_thread and self._scheduler_thread.is_alive()
            )

            channels: list[dict[str, Any]] = []
            for name in sorted(self._channel_state):
                state = self._channel_state[name]
                channels.append(
                    {
                        "name": name,
                        "running": bool(state["running"]),
                        "detail": state["detail"],
                        "updated_at": state["updated_at"],
                    }
                )

        now = self._now_fn()
        return {
            "started_at": self._started_at,
            "uptime_seconds": max(0, int(now - self._started_at)),
            "sessions": stats,
            "scheduler": {
                "running": scheduler_running,
            },
            "channels": channels,
        }

    # --- Internals ---

    def _set_channel_state(self, name: str, running: bool, detail: str) -> None:
        with self._lock:
            self._channel_state[name] = {
                "running": running,
                "detail": detail,
                "updated_at": self._now_fn(),
            }

    @staticmethod
    def _session_summary(session: Session) -> dict:
        return {
            "session_id": session.session_id,
            "sender_id": session.sender_id,
            "title": session.title,
            "created_at": session.created_at,
            "last_active": session.last_active,
            "message_count": len(session.messages),
        }
