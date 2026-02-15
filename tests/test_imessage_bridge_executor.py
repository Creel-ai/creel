"""Tests for the imessage_bridge executor."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from executors.imessage_bridge.executor import call_bridge, get_recent, send_message, get_chats


class TestBridgeClient:
    """Test the bridge client functionality in the imessage_bridge executor."""

    @patch("executors.imessage_bridge.executor.requests.post")
    def test_call_bridge_success(self, mock_post):
        """Test successful bridge call."""
        # Mock successful HTTP response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "success"}
        mock_post.return_value = mock_response

        # Mock environment variables
        with patch.dict(os.environ, {
            "BRIDGE_URL": "http://localhost:8099",
            "BRIDGE_TOKEN": "test-token"
        }):
            result = call_bridge("/imessage/recent")

        assert result["ok"] is True
        assert result["output"] == "success"

        # Verify correct HTTP request was made
        mock_post.assert_called_once_with(
            "http://localhost:8099/imessage/recent",
            json={},
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            timeout=30
        )

    def test_call_bridge_missing_url(self):
        """Test that missing BRIDGE_URL raises error."""
        with patch.dict(os.environ, {"BRIDGE_TOKEN": "test-token"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_URL environment variable not set"):
                call_bridge("/imessage/recent")

    def test_call_bridge_missing_token(self):
        """Test that missing BRIDGE_TOKEN raises error."""
        with patch.dict(os.environ, {"BRIDGE_URL": "http://localhost:8099"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_TOKEN environment variable not set"):
                call_bridge("/imessage/recent")

    @patch("executors.imessage_bridge.executor.requests.post")
    def test_call_bridge_api_error(self, mock_post):
        """Test handling of bridge API errors."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": False, "error": "Command failed"}
        mock_post.return_value = mock_response

        with patch.dict(os.environ, {
            "BRIDGE_URL": "http://localhost:8099",
            "BRIDGE_TOKEN": "test-token"
        }):
            with pytest.raises(RuntimeError, match="Bridge error: Command failed"):
                call_bridge("/imessage/recent")


class TestIMessageOperations:
    """Test iMessage operations that call the bridge."""

    @patch("executors.imessage_bridge.executor.call_bridge")
    def test_get_recent_default(self, mock_call_bridge):
        """Test get_recent function with default limit."""
        mock_call_bridge.return_value = {"ok": True, "output": "recent messages"}

        result = get_recent()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/imessage/recent", {"limit": 20})

    @patch("executors.imessage_bridge.executor.call_bridge")
    def test_get_recent_custom_limit(self, mock_call_bridge):
        """Test get_recent function with custom limit."""
        mock_call_bridge.return_value = {"ok": True, "output": "recent messages"}

        result = get_recent(10)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/imessage/recent", {"limit": 10})

    @patch("executors.imessage_bridge.executor.call_bridge")
    def test_send_message(self, mock_call_bridge):
        """Test send_message function."""
        mock_call_bridge.return_value = {"ok": True, "output": "message sent"}

        result = send_message("friend@example.com", "Hello world")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/imessage/send", {
            "to": "friend@example.com",
            "text": "Hello world"
        })

    @patch("executors.imessage_bridge.executor.call_bridge")
    def test_get_chats(self, mock_call_bridge):
        """Test get_chats function."""
        mock_call_bridge.return_value = {"ok": True, "output": "chat list"}

        result = get_chats()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/imessage/chats")


class TestMainFunction:
    """Test the main executor function."""

    @patch("executors.imessage_bridge.executor.get_recent")
    @patch("builtins.print")
    def test_main_recent_action(self, mock_print, mock_get_recent):
        """Test main function with recent action."""
        mock_get_recent.return_value = {"ok": True, "output": "recent messages"}

        with patch.dict(os.environ, {"ACTION": "recent"}):
            from executors.imessage_bridge.executor import main
            main()

        mock_get_recent.assert_called_once_with(20)
        mock_print.assert_called_once_with("recent messages")

    @patch("executors.imessage_bridge.executor.get_recent")
    @patch("builtins.print")
    def test_main_recent_custom_limit(self, mock_print, mock_get_recent):
        """Test main function with recent action and custom limit."""
        mock_get_recent.return_value = {"ok": True, "output": "recent messages"}

        with patch.dict(os.environ, {"ACTION": "recent", "LIMIT": "10"}):
            from executors.imessage_bridge.executor import main
            main()

        mock_get_recent.assert_called_once_with(10)
        mock_print.assert_called_once_with("recent messages")

    @patch("executors.imessage_bridge.executor.send_message")
    @patch("builtins.print")
    def test_main_send_action(self, mock_print, mock_send_message):
        """Test main function with send action."""
        mock_send_message.return_value = {"ok": True, "output": "message sent"}

        with patch.dict(os.environ, {
            "ACTION": "send",
            "TO": "friend@example.com",
            "TEXT": "Hello world"
        }):
            from executors.imessage_bridge.executor import main
            main()

        mock_send_message.assert_called_once_with("friend@example.com", "Hello world")
        mock_print.assert_called_once_with("message sent")

    @patch("builtins.print")
    def test_main_send_missing_to(self, mock_print):
        """Test main function with send action but missing TO."""
        with patch.dict(os.environ, {"ACTION": "send", "TEXT": "Hello"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.imessage_bridge.executor import main
                main()

        assert excinfo.value.code == 1

    @patch("builtins.print")
    def test_main_send_missing_text(self, mock_print):
        """Test main function with send action but missing TEXT."""
        with patch.dict(os.environ, {"ACTION": "send", "TO": "friend@example.com"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.imessage_bridge.executor import main
                main()

        assert excinfo.value.code == 1

    @patch("executors.imessage_bridge.executor.get_chats")
    @patch("builtins.print")
    def test_main_chats_action(self, mock_print, mock_get_chats):
        """Test main function with chats action."""
        mock_get_chats.return_value = {"ok": True, "output": "chat list"}

        with patch.dict(os.environ, {"ACTION": "chats"}):
            from executors.imessage_bridge.executor import main
            main()

        mock_get_chats.assert_called_once()
        mock_print.assert_called_once_with("chat list")

    @patch("builtins.print")
    def test_main_unknown_action(self, mock_print):
        """Test main function with unknown action."""
        with patch.dict(os.environ, {"ACTION": "unknown"}):
            with pytest.raises(SystemExit) as excinfo:
                from executors.imessage_bridge.executor import main
                main()

        assert excinfo.value.code == 1

    @patch("executors.imessage_bridge.executor.get_recent")
    @patch("builtins.print")
    def test_main_default_action(self, mock_print, mock_get_recent):
        """Test main function with no ACTION set (should default to recent)."""
        mock_get_recent.return_value = {"ok": True, "output": "default recent"}

        with patch.dict(os.environ, {}, clear=True):
            from executors.imessage_bridge.executor import main
            main()

        mock_get_recent.assert_called_once_with(20)
        mock_print.assert_called_once_with("default recent")