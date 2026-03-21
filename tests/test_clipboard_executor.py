"""Tests for the clipboard executor."""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from executors.clipboard.executor import call_bridge, read_clipboard, write_clipboard


class TestBridgeClient:
    """Test the bridge client functionality in the clipboard executor."""

    @patch("executors.clipboard.executor.requests.post")
    def test_call_bridge_success(self, mock_post):
        """Test successful bridge call."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "clipboard text"}
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            result = call_bridge("/clipboard/read")

        assert result["ok"] is True
        assert result["output"] == "clipboard text"

        mock_post.assert_called_once_with(
            "http://localhost:8099/clipboard/read",
            json={},
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    @patch("executors.clipboard.executor.requests.post")
    def test_call_bridge_with_data(self, mock_post):
        """Test bridge call with request data."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "Text copied to clipboard"}
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            result = call_bridge("/clipboard/write", {"text": "hello"})

        assert result["ok"] is True

        mock_post.assert_called_once_with(
            "http://localhost:8099/clipboard/write",
            json={"text": "hello"},
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def test_call_bridge_missing_url(self):
        """Test that missing BRIDGE_URL raises error."""
        with patch.dict(os.environ, {"BRIDGE_TOKEN": "test-token"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_URL environment variable not set"):
                call_bridge("/clipboard/read")

    def test_call_bridge_missing_token(self):
        """Test that missing BRIDGE_TOKEN raises error."""
        with patch.dict(os.environ, {"BRIDGE_URL": "http://localhost:8099"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_TOKEN environment variable not set"):
                call_bridge("/clipboard/read")

    @patch("executors.clipboard.executor.requests.post")
    def test_call_bridge_http_error(self, mock_post):
        """Test handling of HTTP errors."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError("Connection failed")

        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            with pytest.raises(RuntimeError, match="Bridge request failed"):
                call_bridge("/clipboard/read")

    @patch("executors.clipboard.executor.requests.post")
    def test_call_bridge_api_error(self, mock_post):
        """Test handling of bridge API errors."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": False, "error": "pbpaste failed"}
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            with pytest.raises(RuntimeError, match="Bridge error: pbpaste failed"):
                call_bridge("/clipboard/read")


class TestClipboardOperations:
    """Test clipboard read/write operations that call the bridge."""

    @patch("executors.clipboard.executor.call_bridge")
    def test_read_clipboard(self, mock_call_bridge):
        """Test read_clipboard function."""
        mock_call_bridge.return_value = {"ok": True, "output": "copied text"}

        result = read_clipboard()

        assert result["ok"] is True
        assert result["output"] == "copied text"
        mock_call_bridge.assert_called_once_with("/clipboard/read")

    @patch("executors.clipboard.executor.call_bridge")
    def test_write_clipboard(self, mock_call_bridge):
        """Test write_clipboard function."""
        mock_call_bridge.return_value = {"ok": True, "output": "Text copied to clipboard"}

        result = write_clipboard("hello world")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/clipboard/write", {"text": "hello world"})

    @patch("executors.clipboard.executor.call_bridge")
    def test_write_clipboard_empty_string(self, mock_call_bridge):
        """Test write_clipboard with empty string."""
        mock_call_bridge.return_value = {"ok": True, "output": "Text copied to clipboard"}

        result = write_clipboard("")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/clipboard/write", {"text": ""})


class TestMainFunction:
    """Test the main executor function."""

    @patch("executors.clipboard.executor.read_clipboard")
    @patch("builtins.print")
    def test_main_read_action(self, mock_print, mock_read):
        """Test main function with read action."""
        mock_read.return_value = {"ok": True, "output": "clipboard content"}

        with patch.dict(os.environ, {"ACTION": "read"}):
            from executors.clipboard.executor import main

            main()

        mock_read.assert_called_once()
        mock_print.assert_called_once_with("clipboard content")

    @patch("executors.clipboard.executor.write_clipboard")
    @patch("builtins.print")
    def test_main_write_action(self, mock_print, mock_write):
        """Test main function with write action."""
        mock_write.return_value = {"ok": True, "output": "Text copied to clipboard"}

        with patch.dict(os.environ, {"ACTION": "write", "TEXT": "hello"}):
            from executors.clipboard.executor import main

            main()

        mock_write.assert_called_once_with("hello")
        mock_print.assert_called_once_with("Text copied to clipboard")

    @patch("builtins.print")
    def test_main_write_missing_text(self, mock_print):
        """Test main function with write action but missing TEXT."""
        with patch.dict(os.environ, {"ACTION": "write"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.clipboard.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("builtins.print")
    def test_main_unknown_action(self, mock_print):
        """Test main function with unknown action."""
        with patch.dict(os.environ, {"ACTION": "unknown"}):
            with pytest.raises(SystemExit) as excinfo:
                from executors.clipboard.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.clipboard.executor.read_clipboard")
    @patch("builtins.print")
    def test_main_default_action(self, mock_print, mock_read):
        """Test main function with no ACTION set (should default to read)."""
        mock_read.return_value = {"ok": True, "output": "default clipboard"}

        with patch.dict(os.environ, {}, clear=True):
            from executors.clipboard.executor import main

            main()

        mock_read.assert_called_once()
        mock_print.assert_called_once_with("default clipboard")


class TestRegisterSkill:
    """Test skill registration."""

    def test_register_skill_returns_meta_and_execute(self):
        """Test that register_skill returns proper meta and execute function."""
        from executors.clipboard.executor import register_skill

        meta, execute = register_skill()

        assert meta.id == "clipboard"
        assert meta.label == "Clipboard"
        assert meta.needs_bridge is True
        assert meta.bridge_scope == "CLIPBOARD"
        assert meta.platform == "darwin"
        assert len(meta.tools) == 2

        tool_names = {t.name for t in meta.tools}
        assert tool_names == {"read_clipboard", "write_clipboard"}

    @patch("executors.clipboard.executor.read_clipboard")
    def test_execute_read(self, mock_read):
        """Test execute function with read action."""
        mock_read.return_value = {"ok": True, "output": "text"}

        from executors.clipboard.executor import register_skill

        _, execute = register_skill()

        config = MagicMock()
        config.args = {"action": "read"}
        result = execute(config)

        parsed = json.loads(result)
        assert parsed["ok"] is True
        mock_read.assert_called_once()

    @patch("executors.clipboard.executor.write_clipboard")
    def test_execute_write(self, mock_write):
        """Test execute function with write action."""
        mock_write.return_value = {"ok": True, "output": "Text copied to clipboard"}

        from executors.clipboard.executor import register_skill

        _, execute = register_skill()

        config = MagicMock()
        config.args = {"action": "write", "text": "hello"}
        result = execute(config)

        parsed = json.loads(result)
        assert parsed["ok"] is True
        mock_write.assert_called_once_with("hello")

    def test_execute_unknown_action(self):
        """Test execute function with unknown action raises ValueError."""
        from executors.clipboard.executor import register_skill

        _, execute = register_skill()

        config = MagicMock()
        config.args = {"action": "invalid"}
        with pytest.raises(ValueError, match="Unknown clipboard action: invalid"):
            execute(config)


class TestBridgeClipboardEndpoints:
    """Test clipboard endpoints on the bridge server."""

    @pytest.fixture
    def client(self):
        """Test client for the bridge server."""
        from fastapi.testclient import TestClient

        from bridge.server import app

        with TestClient(app) as client:
            yield client

    @pytest.fixture
    def scoped_tokens(self):
        """Mock scoped tokens including CLIPBOARD."""
        tokens = {
            "CLIPBOARD": "test-clipboard-token-123",
            "NOTES": "test-notes-token-123",
        }
        with patch("bridge.server.SCOPED_TOKENS", tokens):
            yield tokens

    @pytest.fixture
    def clipboard_auth_headers(self, scoped_tokens):
        """Authentication headers for clipboard endpoints."""
        return {"Authorization": f"Bearer {scoped_tokens['CLIPBOARD']}"}

    @patch("bridge.server.subprocess.run")
    def test_clipboard_read_success(self, mock_run, client, clipboard_auth_headers):
        """Test successful clipboard read via pbpaste."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "clipboard content here"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        response = client.post("/clipboard/read", headers=clipboard_auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["output"] == "clipboard content here"

    @patch("bridge.server.subprocess.run")
    def test_clipboard_read_empty(self, mock_run, client, clipboard_auth_headers):
        """Test clipboard read when clipboard is empty."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        response = client.post("/clipboard/read", headers=clipboard_auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["output"] == ""

    @patch("bridge.server.subprocess.run")
    def test_clipboard_write_success(self, mock_run, client, clipboard_auth_headers):
        """Test successful clipboard write via pbcopy."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        response = client.post(
            "/clipboard/write",
            headers=clipboard_auth_headers,
            json={"text": "hello world"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["output"] == "Text copied to clipboard"

        mock_run.assert_called_once_with(
            ["pbcopy"],
            input="hello world",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    @patch("bridge.server.subprocess.run")
    def test_clipboard_write_failure(self, mock_run, client, clipboard_auth_headers):
        """Test clipboard write when pbcopy fails."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "some error"
        mock_run.return_value = mock_result

        response = client.post(
            "/clipboard/write",
            headers=clipboard_auth_headers,
            json={"text": "hello"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "pbcopy failed" in data["error"]

    @patch("bridge.server.subprocess.run")
    def test_clipboard_write_timeout(self, mock_run, client, clipboard_auth_headers):
        """Test clipboard write when pbcopy times out."""
        mock_run.side_effect = subprocess.TimeoutExpired(["pbcopy"], 30)

        response = client.post(
            "/clipboard/write",
            headers=clipboard_auth_headers,
            json={"text": "hello"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "timed out" in data["error"]

    @patch("bridge.server.subprocess.run")
    def test_clipboard_write_not_found(self, mock_run, client, clipboard_auth_headers):
        """Test clipboard write when pbcopy is not found."""
        mock_run.side_effect = FileNotFoundError()

        response = client.post(
            "/clipboard/write",
            headers=clipboard_auth_headers,
            json={"text": "hello"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "not found" in data["error"]

    def test_clipboard_missing_auth(self, client):
        """Test that clipboard endpoints reject unauthenticated requests."""
        response = client.post("/clipboard/read")
        assert response.status_code == 401

    def test_clipboard_wrong_scope(self, client, scoped_tokens):
        """Test that wrong scope token is rejected for clipboard."""
        headers = {"Authorization": f"Bearer {scoped_tokens['NOTES']}"}
        response = client.post("/clipboard/read", headers=headers)
        assert response.status_code == 401
