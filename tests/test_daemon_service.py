"""Tests for daemon service extraction (Phase 0/1)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from taskrunner.channels.base import Channel
from taskrunner.daemon.service import DaemonService
from taskrunner.session import SessionManager


class _StubChatServer:
    """Minimal chat-server shape used by DaemonService tests."""

    def __init__(self, sessions_dir: Path) -> None:
        self._session_mgr = SessionManager(sessions_dir=str(sessions_dir), max_history=50)
        self.calls: list[tuple[str, str]] = []

    def handle_message(self, sender_id: str, text: str) -> str:
        self.calls.append((sender_id, text))
        session = self._session_mgr.add_user_message(sender_id, text)
        session.messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": f"echo:{text}"}],
            }
        )
        self._session_mgr.save_session(session)
        return f"echo:{text}"


class _StubChannel(Channel):
    """Controllable channel for lifecycle tests."""

    def __init__(self) -> None:
        self._stop_requested = False
        self.started = threading.Event()
        self.stopped = threading.Event()

    def listen(self, callback):
        self.started.set()
        while not self._stop_requested:
            time.sleep(0.01)
        self.stopped.set()

    def send(self, recipient: str, text: str) -> None:
        return None


@pytest.fixture
def daemon_service(minimal_agent_def, tmp_path: Path) -> DaemonService:
    server = _StubChatServer(tmp_path / "sessions")
    return DaemonService(minimal_agent_def, server=server)


def test_send_message_and_history(daemon_service: DaemonService) -> None:
    response = daemon_service.send_message("cli", "hello")
    assert response == "echo:hello"

    history = daemon_service.get_history("cli", limit=2)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"


def test_new_resume_and_list_sessions(daemon_service: DaemonService) -> None:
    first = daemon_service.new_session("cli")
    daemon_service.send_message("cli", "first")
    second = daemon_service.new_session("cli")
    daemon_service.send_message("cli", "second")

    assert first["session_id"] != second["session_id"]

    sessions = daemon_service.list_sessions("cli")
    session_ids = {s["session_id"] for s in sessions}
    assert first["session_id"] in session_ids
    assert second["session_id"] in session_ids

    resumed = daemon_service.resume_session("cli", first["session_id"])
    assert resumed["session_id"] == first["session_id"]


def test_status_includes_session_counts(daemon_service: DaemonService) -> None:
    daemon_service.send_message("cli", "hello")
    daemon_service.send_message("phone", "hi")

    status = daemon_service.status()
    assert status["sessions"]["stored"] == 2
    assert status["sessions"]["active_senders"] == 2
    assert status["scheduler"]["running"] is False


def test_scheduler_lifecycle(daemon_service: DaemonService) -> None:
    started = threading.Event()

    def _fake_scheduler(tasks_dir, use_containers, shutdown_event):
        started.set()
        shutdown_event.wait(timeout=2)

    with patch("taskrunner.daemon.service.start_scheduler", side_effect=_fake_scheduler):
        assert daemon_service.start_scheduler("tasks") is True
        assert started.wait(timeout=1)
        assert daemon_service.status()["scheduler"]["running"] is True

        assert daemon_service.stop_scheduler(timeout=1) is True
        assert daemon_service.status()["scheduler"]["running"] is False


def test_channel_lifecycle(daemon_service: DaemonService) -> None:
    channel = _StubChannel()
    daemon_service.register_channel("imessage", channel)

    assert daemon_service.start_channel("imessage") is True
    assert channel.started.wait(timeout=1)

    running_state = next(
        c for c in daemon_service.status()["channels"] if c["name"] == "imessage"
    )
    assert running_state["running"] is True

    assert daemon_service.stop_channel("imessage", timeout=1) is True
    assert channel.stopped.wait(timeout=1)

    stopped_state = next(
        c for c in daemon_service.status()["channels"] if c["name"] == "imessage"
    )
    assert stopped_state["running"] is False
