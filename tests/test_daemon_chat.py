"""Tests for the chat UI endpoints (HTTP + WebSocket)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from creel.daemon.api import create_daemon_app
from creel.daemon.service import DaemonService
from creel.session import SessionManager


class _StubChatServer:
    """Minimal stub that echoes messages for testing."""

    def __init__(self, sessions_dir: Path) -> None:
        self._session_mgr = SessionManager(sessions_dir=str(sessions_dir), max_history=50)
        self._guardian = None
        self._cron_manager = None

    def handle_message(
        self,
        sender_id: str,
        text: str,
        on_text_delta=None,
        *,
        auto_approve: bool = False,
        attachments=None,
        channel: str = "test",
    ) -> str:
        session = self._session_mgr.add_user_message(sender_id, text)
        response = f"echo:{text}"
        if on_text_delta is not None:
            on_text_delta("echo:")
            on_text_delta(text)
        session.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": response}]}
        )
        self._session_mgr.save_session(session)
        return response


@pytest.fixture()
def client(minimal_agent_def, tmp_path: Path) -> TestClient:
    server = _StubChatServer(tmp_path / "sessions")
    service = DaemonService(minimal_agent_def, server=server)
    app = create_daemon_app(service)
    with TestClient(app) as c:
        yield c


# --- HTTP endpoints ---


def test_chat_ui_serves_html(client: TestClient) -> None:
    resp = client.get("/chat")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Creel Chat" in resp.text


def test_chat_sessions_empty(client: TestClient) -> None:
    resp = client.get("/chat/sessions", params={"sender_id": "web-test"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_chat_send_and_sessions(client: TestClient) -> None:
    resp = client.post("/chat/send", json={"sender_id": "web-test", "text": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "echo:hello"
    assert body["session_id"] is not None

    sessions = client.get("/chat/sessions", params={"sender_id": "web-test"})
    assert sessions.status_code == 200
    assert len(sessions.json()) >= 1


def test_chat_send_empty(client: TestClient) -> None:
    resp = client.post("/chat/send", json={"sender_id": "web-test", "text": "   "})
    assert resp.status_code == 200
    assert resp.json()["error"] == "empty message"


# --- WebSocket endpoint ---


def test_ws_connect_and_list_sessions(client: TestClient) -> None:
    with client.websocket_connect("/chat/ws") as ws:
        ws.send_json({"type": "sessions", "sender_id": "ws-test"})
        data = ws.receive_json()
        assert data["type"] == "sessions"
        assert isinstance(data["sessions"], list)


def test_ws_new_session(client: TestClient) -> None:
    with client.websocket_connect("/chat/ws") as ws:
        ws.send_json({"type": "new_session", "sender_id": "ws-test"})
        data = ws.receive_json()
        assert data["type"] == "session_created"
        assert "session_id" in data


def test_ws_send_message_streaming(client: TestClient) -> None:
    with client.websocket_connect("/chat/ws") as ws:
        ws.send_json({"type": "message", "text": "hi", "sender_id": "ws-test"})

        events = []
        while True:
            data = ws.receive_json()
            events.append(data)
            if data["type"] in ("final", "error"):
                break

        types = [e["type"] for e in events]
        assert "start" in types
        assert "final" in types

        final = next(e for e in events if e["type"] == "final")
        assert final["text"] == "echo:hi"

        token_texts = [e["text"] for e in events if e["type"] == "token"]
        assert token_texts == ["echo:", "hi"]


def test_ws_resume_and_history(client: TestClient) -> None:
    with client.websocket_connect("/chat/ws") as ws:
        # Create a session and send a message
        ws.send_json({"type": "new_session", "sender_id": "ws-test"})
        created = ws.receive_json()
        session_id = created["session_id"]

        ws.send_json(
            {
                "type": "message",
                "text": "test msg",
                "sender_id": "ws-test",
                "session_id": session_id,
            }
        )
        # Drain streaming events
        while True:
            data = ws.receive_json()
            if data["type"] in ("final", "error"):
                break

        # Request history
        ws.send_json(
            {
                "type": "history",
                "sender_id": "ws-test",
                "session_id": session_id,
                "limit": 10,
            }
        )
        history = ws.receive_json()
        assert history["type"] == "history"
        assert len(history["messages"]) >= 2

        # Resume session
        ws.send_json(
            {
                "type": "resume_session",
                "sender_id": "ws-test",
                "session_id": session_id,
            }
        )
        resumed = ws.receive_json()
        assert resumed["type"] == "session_resumed"
        assert resumed["session_id"] == session_id


def test_ws_empty_message_rejected(client: TestClient) -> None:
    with client.websocket_connect("/chat/ws") as ws:
        ws.send_json({"type": "message", "text": "  ", "sender_id": "ws-test"})
        data = ws.receive_json()
        assert data["type"] == "error"
        assert "empty" in data["error"]


def test_ws_invalid_json(client: TestClient) -> None:
    with client.websocket_connect("/chat/ws") as ws:
        ws.send_text("not json{{{")
        data = ws.receive_json()
        assert data["type"] == "error"
        assert "invalid JSON" in data["error"]


def test_ws_unknown_message_type(client: TestClient) -> None:
    with client.websocket_connect("/chat/ws") as ws:
        ws.send_json({"type": "foobar"})
        data = ws.receive_json()
        assert data["type"] == "error"
        assert "unknown" in data["error"]
