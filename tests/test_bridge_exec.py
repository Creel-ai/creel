"""Tests for bridge exec/process/sessions endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bridge.process_manager import ProcessManager  # noqa: F401
from bridge.server import app


@pytest.fixture
def exec_tokens():
    """Mock scoped tokens including EXEC scope."""
    tokens = {
        "NOTES": "test-notes-token-123",
        "REMINDERS": "test-reminders-token-123",
        "THINGS": "test-things-token-123",
        "IMESSAGE": "test-imessage-token-123",
        "BROWSER": "test-browser-token-123",
        "GIT": "test-git-token-123",
        "EXEC": "test-exec-token-123",
    }
    with patch("bridge.server.SCOPED_TOKENS", tokens):
        yield tokens


@pytest.fixture
def exec_auth_headers(exec_tokens):
    """Authentication headers for exec endpoints."""
    return {"Authorization": f"Bearer {exec_tokens['EXEC']}"}


@pytest.fixture
def process_manager():
    """Create a real ProcessManager for testing."""
    pm = ProcessManager(max_sessions=10, max_age_hours=4)
    yield pm
    pm.shutdown()


@pytest.fixture
def client(process_manager):
    """Test client with ProcessManager attached."""
    app.state.process_manager = process_manager
    with TestClient(app) as client:
        yield client


class TestExecEndpointAuth:
    """Test authentication for exec endpoints."""

    def test_exec_no_auth(self, client):
        response = client.post("/exec", json={"command": "echo test"})
        assert response.status_code == 401

    def test_exec_wrong_token(self, client, exec_tokens):
        headers = {"Authorization": f"Bearer {exec_tokens['NOTES']}"}
        response = client.post("/exec", json={"command": "echo test"}, headers=headers)
        assert response.status_code == 401

    def test_process_no_auth(self, client):
        response = client.post("/process", json={"session_id": "x", "action": "poll"})
        assert response.status_code == 401

    def test_sessions_no_auth(self, client):
        response = client.get("/sessions")
        assert response.status_code == 401


class TestExecEndpoint:
    """Test POST /exec endpoint."""

    def test_foreground_exec(self, client, exec_auth_headers):
        response = client.post(
            "/exec",
            json={"command": "echo hello", "background": False, "timeout": 10},
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "exited"
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

    def test_background_exec(self, client, exec_auth_headers):
        response = client.post(
            "/exec",
            json={"command": "sleep 10", "background": True, "timeout": 30},
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "running"
        assert "session_id" in result
        assert "pid" in result

    def test_exec_with_workdir(self, client, exec_auth_headers, tmp_path):
        response = client.post(
            "/exec",
            json={
                "command": "pwd",
                "background": False,
                "workdir": str(tmp_path),
                "timeout": 10,
            },
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert str(tmp_path) in result["stdout"]

    def test_exec_empty_command(self, client, exec_auth_headers):
        response = client.post(
            "/exec",
            json={"command": ""},
            headers=exec_auth_headers,
        )
        # Pydantic validation should reject empty command
        assert response.status_code == 422

    def test_exec_invalid_workdir(self, client, exec_auth_headers):
        response = client.post(
            "/exec",
            json={
                "command": "echo test",
                "workdir": "/nonexistent/path/xyz",
                "timeout": 10,
            },
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert "does not exist" in result["error"]

    def test_exec_with_env(self, client, exec_auth_headers):
        response = client.post(
            "/exec",
            json={
                "command": "echo $MY_TEST_VAR",
                "background": False,
                "timeout": 10,
                "env": {"MY_TEST_VAR": "test_value"},
            },
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert "test_value" in result["stdout"]

    def test_exec_workdir_traversal_rejected(self, client, exec_auth_headers):
        response = client.post(
            "/exec",
            json={
                "command": "echo test",
                "workdir": "/tmp/../etc/passwd",
            },
            headers=exec_auth_headers,
        )
        assert response.status_code == 422


class TestProcessEndpoint:
    """Test POST /process endpoint."""

    def test_poll_action(self, client, exec_auth_headers):
        # Spawn a background process first
        spawn_response = client.post(
            "/exec",
            json={"command": "sleep 10", "background": True, "timeout": 30},
            headers=exec_auth_headers,
        )
        sid = spawn_response.json()["session_id"]

        # Poll it
        response = client.post(
            "/process",
            json={"session_id": sid, "action": "poll"},
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "running"

    def test_log_action(self, client, exec_auth_headers):
        import time

        spawn_response = client.post(
            "/exec",
            json={
                "command": 'echo "test line" && sleep 0.5',
                "background": True,
                "timeout": 10,
            },
            headers=exec_auth_headers,
        )
        sid = spawn_response.json()["session_id"]
        time.sleep(1)

        response = client.post(
            "/process",
            json={"session_id": sid, "action": "log", "limit": 50},
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert "lines" in result
        assert any("test line" in line for line in result["lines"])

    def test_kill_action(self, client, exec_auth_headers):
        spawn_response = client.post(
            "/exec",
            json={"command": "sleep 300", "background": True, "timeout": 600},
            headers=exec_auth_headers,
        )
        sid = spawn_response.json()["session_id"]

        response = client.post(
            "/process",
            json={"session_id": sid, "action": "kill"},
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "killed"

    def test_write_action(self, client, exec_auth_headers):
        import time

        spawn_response = client.post(
            "/exec",
            json={"command": "cat", "background": True, "timeout": 10},
            headers=exec_auth_headers,
        )
        sid = spawn_response.json()["session_id"]
        time.sleep(0.3)

        response = client.post(
            "/process",
            json={"session_id": sid, "action": "write", "data": "hello"},
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["written"] > 0

    def test_write_without_data(self, client, exec_auth_headers):
        spawn_response = client.post(
            "/exec",
            json={"command": "sleep 10", "background": True, "timeout": 30},
            headers=exec_auth_headers,
        )
        sid = spawn_response.json()["session_id"]

        response = client.post(
            "/process",
            json={"session_id": sid, "action": "write"},
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert "data is required" in result["error"]

    def test_nonexistent_session(self, client, exec_auth_headers):
        response = client.post(
            "/process",
            json={"session_id": "nonexistent-99", "action": "poll"},
            headers=exec_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_invalid_action(self, client, exec_auth_headers):
        response = client.post(
            "/process",
            json={"session_id": "test-1", "action": "invalid"},
            headers=exec_auth_headers,
        )
        # Pydantic validation rejects invalid action
        assert response.status_code == 422


class TestSessionsEndpoint:
    """Test GET /sessions endpoint."""

    def test_list_empty(self, client, exec_auth_headers):
        response = client.get("/sessions", headers=exec_auth_headers)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["sessions"] == []

    def test_list_with_sessions(self, client, exec_auth_headers):
        client.post(
            "/exec",
            json={"command": "sleep 10", "background": True, "timeout": 30},
            headers=exec_auth_headers,
        )
        client.post(
            "/exec",
            json={"command": "sleep 10", "background": True, "timeout": 30},
            headers=exec_auth_headers,
        )

        response = client.get("/sessions", headers=exec_auth_headers)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert len(result["sessions"]) == 2


class TestExecAllowedWorkdirs:
    """Test EXEC_ALLOWED_WORKDIRS env var integration."""

    def test_allowed_workdir_via_env(self, exec_tokens, tmp_path):
        env = {"EXEC_ALLOWED_WORKDIRS": str(tmp_path)}
        with patch.dict("os.environ", env):
            with TestClient(app) as client:
                headers = {"Authorization": f"Bearer {exec_tokens['EXEC']}"}
                response = client.post(
                    "/exec",
                    json={
                        "command": "pwd",
                        "background": False,
                        "workdir": str(tmp_path),
                        "timeout": 10,
                    },
                    headers=headers,
                )
                assert response.status_code == 200
                result = response.json()
                assert result["ok"] is True

    def test_disallowed_workdir_via_env(self, exec_tokens, tmp_path):
        env = {"EXEC_ALLOWED_WORKDIRS": "/some/other/dir"}
        with patch.dict("os.environ", env):
            with TestClient(app) as client:
                headers = {"Authorization": f"Bearer {exec_tokens['EXEC']}"}
                response = client.post(
                    "/exec",
                    json={
                        "command": "pwd",
                        "background": False,
                        "workdir": str(tmp_path),
                        "timeout": 10,
                    },
                    headers=headers,
                )
                assert response.status_code == 200
                result = response.json()
                assert result["ok"] is False
                assert "not under any allowed prefix" in result["error"]


class TestExecScopedAuth:
    """Test that EXEC scope is properly isolated."""

    def test_notes_token_cannot_exec(self, client, exec_tokens):
        headers = {"Authorization": f"Bearer {exec_tokens['NOTES']}"}
        response = client.post(
            "/exec",
            json={"command": "echo test"},
            headers=headers,
        )
        assert response.status_code == 401

    def test_exec_token_cannot_notes(self, client, exec_tokens):
        headers = {"Authorization": f"Bearer {exec_tokens['EXEC']}"}
        response = client.post("/notes/list", headers=headers)
        assert response.status_code == 401
