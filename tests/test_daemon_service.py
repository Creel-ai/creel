"""Tests for daemon service extraction (Phase 0/1)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from creel.channels.base import Channel
from creel.daemon.service import DaemonService
from creel.session import SessionManager


class _StubChatServer:
    """Minimal chat-server shape used by DaemonService tests."""

    def __init__(self, sessions_dir: Path) -> None:
        self._session_mgr = SessionManager(sessions_dir=str(sessions_dir))
        self._guardian = None
        self._cron_manager = None
        self.calls: list[tuple[str, str]] = []
        self._interrupt_calls: list[str] = []
        self._active_senders: set[str] = set()

    def handle_message(
        self,
        sender_id: str,
        text: str,
        on_text_delta=None,
        *,
        auto_approve: bool = False,
        attachments=None,
        channel: str = "unknown",
    ) -> str:
        self.calls.append((sender_id, text))
        session = self._session_mgr.add_user_message(sender_id, text)
        response = f"echo:{text}"
        if on_text_delta is not None:
            on_text_delta("echo:")
            on_text_delta(text)
        session.messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": response}],
            }
        )
        self._session_mgr.save_session(session)
        return response

    def interrupt_sender(self, sender_id: str) -> bool:
        self._interrupt_calls.append(sender_id)
        return sender_id in self._active_senders


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


def test_stream_message_events(daemon_service: DaemonService) -> None:
    events = list(daemon_service.stream_message("cli", "hello"))

    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "final"
    assert events[-1]["payload"]["text"] == "echo:hello"

    token_chunks = [e["payload"]["text"] for e in events if e["type"] == "token"]
    token_text = "".join(token_chunks)
    assert token_chunks == ["echo:", "hello"]
    assert token_text == "echo:hello"


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

    with patch("creel.daemon.service.start_scheduler", side_effect=_fake_scheduler):
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

    running_state = next(c for c in daemon_service.status()["channels"] if c["name"] == "imessage")
    assert running_state["running"] is True

    assert daemon_service.stop_channel("imessage", timeout=1) is True
    assert channel.stopped.wait(timeout=1)

    stopped_state = next(c for c in daemon_service.status()["channels"] if c["name"] == "imessage")
    assert stopped_state["running"] is False


# ---------------------------------------------------------------------------
# Interrupt tests
# ---------------------------------------------------------------------------


def test_interrupt_sender_delegates_to_server(daemon_service: DaemonService) -> None:
    daemon_service._server._active_senders.add("cli")
    result = daemon_service.interrupt_sender("cli")
    assert result is True
    assert "cli" in daemon_service._server._interrupt_calls


def test_interrupt_sender_no_active_loop(daemon_service: DaemonService) -> None:
    result = daemon_service.interrupt_sender("nobody")
    assert result is False


def test_send_message_interrupt_word_with_active_loop(daemon_service: DaemonService) -> None:
    """Interrupt word before lock should short-circuit when loop is active."""
    daemon_service._server._active_senders.add("cli")
    result = daemon_service.send_message("cli", "stop")
    assert result == "Stopping..."
    # handle_message should NOT have been called
    assert ("cli", "stop") not in daemon_service._server.calls


def test_send_message_interrupt_word_no_active_loop(daemon_service: DaemonService) -> None:
    """Interrupt word without active loop falls through to normal processing."""
    result = daemon_service.send_message("cli", "stop")
    assert result == "echo:stop"
    assert ("cli", "stop") in daemon_service._server.calls


def test_send_message_incoming_message_interrupt(daemon_service: DaemonService) -> None:
    """Interrupt check works for IncomingMessage objects too."""
    from creel.channels.message import IncomingMessage

    daemon_service._server._active_senders.add("user1")
    incoming = IncomingMessage(sender_id="user1", text="stop", channel="test")
    result = daemon_service.send_message(incoming)
    assert result == "Stopping..."


def test_interrupt_words_cached(daemon_service: DaemonService) -> None:
    assert "stop" in daemon_service._interrupt_words
    assert "cancel" in daemon_service._interrupt_words


def test_start_channel_configures_interrupt(minimal_agent_def, tmp_path: Path) -> None:
    """start_channel should call configure_interrupt on the channel."""
    server = _StubChatServer(tmp_path / "sessions")
    svc = DaemonService(minimal_agent_def, server=server)

    channel = _StubChannel()
    svc.register_channel("test", channel)
    svc.start_channel("test")

    # Wait for channel to start
    assert channel.started.wait(timeout=1)

    assert channel._interrupt_fn is not None
    assert "stop" in channel._interrupt_words

    svc.stop_channel("test", timeout=1)
