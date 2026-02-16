"""Tests for daemon TUI client adapter."""

from __future__ import annotations

from taskrunner.daemon.client import DaemonTuiAdapter


class _FakeClient:
    def __init__(self) -> None:
        self.active = "sess-1"
        self.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]

    def send_message(self, sender_id: str, text: str, session_id: str | None = None):
        return {
            "sender_id": sender_id,
            "text": f"echo:{text}",
            "session_id": self.active,
        }

    def get_active_session(self, sender_id: str):
        return {
            "sender_id": sender_id,
            "session_id": self.active,
            "title": "Test session",
            "created_at": 0.0,
            "last_active": 0.0,
            "message_count": len(self.messages),
        }

    def get_history(self, sender_id: str, session_id: str, limit: int = 100):
        return list(self.messages)[-limit:]

    def new_session(self, sender_id: str):
        self.active = "sess-2"
        self.messages = []
        return {
            "sender_id": sender_id,
            "session_id": self.active,
            "title": "",
            "created_at": 1.0,
            "last_active": 1.0,
            "message_count": 0,
        }

    def resume_session(self, sender_id: str, session_id: str):
        self.active = session_id
        return {
            "sender_id": sender_id,
            "session_id": self.active,
            "title": "Resumed",
            "created_at": 2.0,
            "last_active": 2.0,
            "message_count": len(self.messages),
        }

    def list_sessions(self, sender_id: str):
        return [
            {"session_id": self.active, "title": "A", "message_count": 1},
            {"session_id": "sess-x", "title": "B", "message_count": 2},
        ]


def test_adapter_fetches_active_session_and_history() -> None:
    client = _FakeClient()
    adapter = DaemonTuiAdapter(client, sender_id="cli")

    session = adapter.get_or_create_session("cli")
    assert session.session_id == "sess-1"
    assert session.title == "Test session"
    assert session.messages and len(session.messages) == 2


def test_adapter_send_updates_active_session_id() -> None:
    client = _FakeClient()
    adapter = DaemonTuiAdapter(client, sender_id="cli")

    result = adapter.handle_message("cli", "hello")
    assert result == "echo:hello"


def test_adapter_new_and_resume_session() -> None:
    client = _FakeClient()
    adapter = DaemonTuiAdapter(client, sender_id="cli")

    new_session = adapter.new_session("cli")
    assert new_session.session_id == "sess-2"

    resumed = adapter.resume_session("cli", "sess-9")
    assert resumed.session_id == "sess-9"
