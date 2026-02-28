"""Tests for daemon HTTP API contracts."""

from __future__ import annotations

import textwrap
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskrunner.daemon import api as daemon_api
from taskrunner.daemon.service import DaemonService
from taskrunner.session import SessionManager


class _StubChatServer:
    def __init__(self, sessions_dir: Path) -> None:
        self._session_mgr = SessionManager(
            sessions_dir=str(sessions_dir), max_history=50
        )
        self._guardian = None

    def handle_message(
        self,
        sender_id: str,
        text: str,
        on_text_delta=None,
        *,
        auto_approve: bool = False,
    ) -> str:
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


@pytest.fixture
def client(minimal_agent_def, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    creel_home = tmp_path / ".creel"
    creel_home.mkdir()
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    static_dir = tmp_path / "dashboard-static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><html><body>dashboard</body></html>")

    monkeypatch.setenv("CREEL_HOME", str(creel_home))
    monkeypatch.setenv("CREEL_TASKS_DIR", str(tasks_dir))
    monkeypatch.setattr(daemon_api, "_DASHBOARD_STATIC_DIR", static_dir)

    server = _StubChatServer(tmp_path / "sessions")
    service = DaemonService(minimal_agent_def, server=server)
    app = daemon_api.create_daemon_app(service)
    with TestClient(app) as c:
        yield c


def _auth_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {client.app.state.dashboard_token}"}


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

    resumed = client.post(
        f"/v1/sessions/{session_id}/resume", json={"sender_id": "cli"}
    )
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
    token_chunks = [e["payload"]["text"] for e in events if e["type"] == "token"]
    assert token_chunks == ["echo:", "hello"]


def test_dashboard_status_requires_auth(client: TestClient) -> None:
    unauth = client.get("/api/status")
    assert unauth.status_code == 401

    authed = client.get("/api/status", headers=_auth_headers(client))
    assert authed.status_code == 200
    assert authed.json()["daemon"]["running"] is True


def test_dashboard_logs_recent_requires_auth(client: TestClient) -> None:
    unauth = client.get("/api/logs/recent")
    assert unauth.status_code == 401

    authed = client.get("/api/logs/recent", headers=_auth_headers(client))
    assert authed.status_code == 200
    assert "lines" in authed.json()


def test_spa_fallback_does_not_mask_unknown_api_routes(client: TestClient) -> None:
    resp = client.get("/api/not-a-real-route")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"] == "Not Found"


def test_spa_fallback_does_not_mask_unknown_v1_routes(client: TestClient) -> None:
    resp = client.get("/v1/not-a-real-route")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"] == "Not Found"


def test_spa_fallback_serves_frontend_routes(client: TestClient) -> None:
    resp = client.get("/tasks/some-client-route")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "dashboard" in resp.text


def test_create_task_rejects_raw_yaml_name_mismatch(client: TestClient, tmp_path: Path) -> None:
    payload = {
        "name": "foo",
        "raw_yaml": textwrap.dedent(
            """
            name: bar
            schedule: "0 0 * * *"
            prompt: test
            output:
              type: stdout
              to: ""
            """
        ).lstrip(),
    }
    resp = client.post("/api/tasks", headers=_auth_headers(client), json=payload)
    assert resp.status_code == 400
    assert "must match task name 'foo'" in resp.json()["detail"]
    assert not (tmp_path / "tasks" / "foo.yaml").exists()


def test_update_task_rejects_raw_yaml_name_mismatch(client: TestClient, tmp_path: Path) -> None:
    task_path = tmp_path / "tasks" / "foo.yaml"
    task_path.write_text(
        textwrap.dedent(
            """
            name: foo
            schedule: "0 0 * * *"
            prompt: original
            output:
              type: stdout
              to: ""
            """
        ).lstrip()
    )

    payload = {
        "name": "foo",
        "raw_yaml": textwrap.dedent(
            """
            name: bar
            schedule: "0 0 * * *"
            prompt: test
            output:
              type: stdout
              to: ""
            """
        ).lstrip(),
    }
    resp = client.put("/api/tasks/foo", headers=_auth_headers(client), json=payload)
    assert resp.status_code == 400
    assert "must match task name 'foo'" in resp.json()["detail"]
    assert "name: foo" in task_path.read_text()


def test_update_task_rejects_body_name_mismatch(client: TestClient, tmp_path: Path) -> None:
    task_path = tmp_path / "tasks" / "foo.yaml"
    task_path.write_text(
        textwrap.dedent(
            """
            name: foo
            schedule: "0 0 * * *"
            prompt: original
            output:
              type: stdout
              to: ""
            """
        ).lstrip()
    )

    resp = client.put(
        "/api/tasks/foo",
        headers=_auth_headers(client),
        json={"name": "bar", "prompt": "still-valid", "schedule": "0 0 * * *"},
    )
    assert resp.status_code == 400
    assert "must match route name 'foo'" in resp.json()["detail"]
    assert "name: foo" in task_path.read_text()


# --- Deferred init tests ---


def test_deferred_init_health_starting_then_ok(minimal_agent_def, tmp_path: Path) -> None:
    """Health returns 'starting' before factory completes, then 'ok' after."""
    gate = threading.Event()

    def _factory():
        gate.wait()  # block until test releases
        server = _StubChatServer(tmp_path / "sessions")
        return DaemonService(minimal_agent_def, server=server)

    app = daemon_api.create_daemon_app(init_factory=_factory)
    with TestClient(app, raise_server_exceptions=False) as c:
        # Before init completes
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "starting"

        # Non-health endpoints should 503
        resp = c.get("/v1/status")
        assert resp.status_code == 503

        # Release init
        gate.set()

        # Poll until ready (should be near-instant)
        import time

        deadline = time.time() + 5.0
        while time.time() < deadline:
            resp = c.get("/health")
            if resp.json()["status"] == "ok":
                break
            time.sleep(0.05)

        assert resp.json()["status"] == "ok"

        # Endpoints should now work
        resp = c.get("/v1/status")
        assert resp.status_code == 200


def test_deferred_init_factory_failure(minimal_agent_def) -> None:
    """If init_factory raises, health transitions to 'error' and endpoints stay 503."""

    def _factory():
        raise RuntimeError("init boom")

    app = daemon_api.create_daemon_app(init_factory=_factory)
    with TestClient(app, raise_server_exceptions=False) as c:
        # Poll until failure is detected
        import time

        deadline = time.time() + 5.0
        while time.time() < deadline:
            resp = c.get("/health")
            if resp.json()["status"] == "error":
                break
            time.sleep(0.05)

        body = resp.json()
        assert body["status"] == "error"
        assert "init boom" in body["error"]

        # Non-health endpoints should still 503
        resp = c.get("/v1/status")
        assert resp.status_code == 503
        assert "failed" in resp.json()["detail"].lower()


def test_immediate_init_still_works(minimal_agent_def, tmp_path: Path) -> None:
    """Passing service= directly (the test/simple path) still works as before."""
    server = _StubChatServer(tmp_path / "sessions")
    service = DaemonService(minimal_agent_def, server=server)
    app = daemon_api.create_daemon_app(service)
    with TestClient(app) as c:
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        resp = c.get("/v1/status")
        assert resp.status_code == 200
