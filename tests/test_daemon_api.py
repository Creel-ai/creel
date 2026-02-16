"""Tests for daemon HTTP API contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskrunner.daemon.api import create_daemon_app
from taskrunner.daemon.service import DaemonService
from taskrunner.session import SessionManager


class _StubChatServer:
    def __init__(self, sessions_dir: Path) -> None:
        self._session_mgr = SessionManager(sessions_dir=str(sessions_dir), max_history=50)

    def handle_message(self, sender_id: str, text: str) -> str:
        session = self._session_mgr.add_user_message(sender_id, text)
        session.messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": f"echo:{text}"}],
            }
        )
        self._session_mgr.save_session(session)
        return f"echo:{text}"


@pytest.fixture
def client(minimal_agent_def, tmp_path: Path) -> TestClient:
    server = _StubChatServer(tmp_path / "sessions")
    service = DaemonService(minimal_agent_def, server=server)
    app = create_daemon_app(service)
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_send_message_and_status(client: TestClient) -> None:
    resp = client.post("/v1/messages", json={"sender_id": "cli", "text": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "echo:hello"
    assert body["session_id"] is not None

    status = client.get("/v1/status")
    assert status.status_code == 200
    assert status.json()["sessions"]["stored"] == 1


def test_session_endpoints(client: TestClient) -> None:
    created = client.post("/v1/sessions/new", json={"sender_id": "cli"})
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    sent = client.post(
        "/v1/messages",
        json={"sender_id": "cli", "session_id": session_id, "text": "ping"},
    )
    assert sent.status_code == 200

    sessions = client.get("/v1/sessions", params={"sender_id": "cli"})
    assert sessions.status_code == 200
    assert any(s["session_id"] == session_id for s in sessions.json())

    active = client.get("/v1/sessions/active", params={"sender_id": "cli"})
    assert active.status_code == 200
    assert active.json()["session_id"] == session_id

    resumed = client.post(f"/v1/sessions/{session_id}/resume", json={"sender_id": "cli"})
    assert resumed.status_code == 200
    assert resumed.json()["session_id"] == session_id

    history = client.get(
        f"/v1/sessions/{session_id}/history",
        params={"sender_id": "cli", "limit": 10},
    )
    assert history.status_code == 200
    assert len(history.json()["messages"]) >= 2


def test_stream_message_endpoint(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/messages/stream",
        json={"sender_id": "cli", "text": "hello"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events: list[dict] = []
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line.split(":", 1)[1].strip()
            if payload:
                import json

                events.append(json.loads(payload))

    assert events
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "final"
    assert events[-1]["payload"]["text"] == "echo:hello"
