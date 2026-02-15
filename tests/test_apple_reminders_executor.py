"""Tests for the apple_reminders executor."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from executors.apple_reminders.executor import call_bridge, list_reminders, add_reminder, complete_reminder


class TestBridgeClient:
    """Test the bridge client functionality in the apple_reminders executor."""

    @patch("executors.apple_reminders.executor.requests.post")
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
            result = call_bridge("/reminders/list")

        assert result["ok"] is True
        assert result["output"] == "success"

        # Verify correct HTTP request was made
        mock_post.assert_called_once_with(
            "http://localhost:8099/reminders/list",
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
                call_bridge("/reminders/list")

    def test_call_bridge_missing_token(self):
        """Test that missing BRIDGE_TOKEN raises error."""
        with patch.dict(os.environ, {"BRIDGE_URL": "http://localhost:8099"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_TOKEN environment variable not set"):
                call_bridge("/reminders/list")

    @patch("executors.apple_reminders.executor.requests.post")
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
                call_bridge("/reminders/list")


class TestRemindersOperations:
    """Test reminders operations that call the bridge."""

    @patch("executors.apple_reminders.executor.call_bridge")
    def test_list_reminders_default(self, mock_call_bridge):
        """Test list_reminders function with default filter."""
        mock_call_bridge.return_value = {"ok": True, "output": "reminder1\nreminder2"}

        result = list_reminders()

        assert result["ok"] is True
        assert result["output"] == "reminder1\nreminder2"
        mock_call_bridge.assert_called_once_with("/reminders/list", {"filter": "all"})

    @patch("executors.apple_reminders.executor.call_bridge")
    def test_list_reminders_today(self, mock_call_bridge):
        """Test list_reminders function with today filter."""
        mock_call_bridge.return_value = {"ok": True, "output": "today reminders"}

        result = list_reminders("today")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/reminders/list", {"filter": "today"})

    @patch("executors.apple_reminders.executor.call_bridge")
    def test_list_reminders_week(self, mock_call_bridge):
        """Test list_reminders function with week filter."""
        mock_call_bridge.return_value = {"ok": True, "output": "week reminders"}

        result = list_reminders("week")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/reminders/list", {"filter": "week"})

    @patch("executors.apple_reminders.executor.call_bridge")
    def test_list_reminders_overdue(self, mock_call_bridge):
        """Test list_reminders function with overdue filter."""
        mock_call_bridge.return_value = {"ok": True, "output": "overdue reminders"}

        result = list_reminders("overdue")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/reminders/list", {"filter": "overdue"})

    @patch("executors.apple_reminders.executor.call_bridge")
    def test_add_reminder_basic(self, mock_call_bridge):
        """Test add_reminder function with basic parameters."""
        mock_call_bridge.return_value = {"ok": True, "output": "reminder added"}

        result = add_reminder("Test Reminder")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/reminders/add", 
            {"title": "Test Reminder"}
        )

    @patch("executors.apple_reminders.executor.call_bridge")
    def test_add_reminder_with_list_and_due(self, mock_call_bridge):
        """Test add_reminder function with list and due date."""
        mock_call_bridge.return_value = {"ok": True, "output": "reminder added"}

        result = add_reminder("Test Reminder", "Work", "tomorrow")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/reminders/add", 
            {"title": "Test Reminder", "list": "Work", "due": "tomorrow"}
        )

    @patch("executors.apple_reminders.executor.call_bridge")
    def test_complete_reminder(self, mock_call_bridge):
        """Test complete_reminder function."""
        mock_call_bridge.return_value = {"ok": True, "output": "reminder completed"}

        result = complete_reminder("123")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/reminders/complete", 
            {"id": "123"}
        )


class TestMainFunction:
    """Test the main executor function."""

    @patch("executors.apple_reminders.executor.list_reminders")
    @patch("builtins.print")
    def test_main_list_action(self, mock_print, mock_list_reminders):
        """Test main function with list action."""
        mock_list_reminders.return_value = {"ok": True, "output": "reminder1\nreminder2"}

        with patch.dict(os.environ, {"ACTION": "list"}):
            from executors.apple_reminders.executor import main
            main()

        mock_list_reminders.assert_called_once_with("all")
        mock_print.assert_called_once_with("reminder1\nreminder2")

    @patch("executors.apple_reminders.executor.list_reminders")
    @patch("builtins.print")
    def test_main_list_with_filter(self, mock_print, mock_list_reminders):
        """Test main function with list action and filter."""
        mock_list_reminders.return_value = {"ok": True, "output": "today reminders"}

        with patch.dict(os.environ, {"ACTION": "list", "FILTER": "today"}):
            from executors.apple_reminders.executor import main
            main()

        mock_list_reminders.assert_called_once_with("today")
        mock_print.assert_called_once_with("today reminders")

    @patch("executors.apple_reminders.executor.add_reminder")
    @patch("builtins.print")
    def test_main_add_action(self, mock_print, mock_add_reminder):
        """Test main function with add action."""
        mock_add_reminder.return_value = {"ok": True, "output": "reminder added"}

        with patch.dict(os.environ, {
            "ACTION": "add", 
            "TITLE": "Test Reminder", 
            "LIST": "Work",
            "DUE": "tomorrow"
        }):
            from executors.apple_reminders.executor import main
            main()

        mock_add_reminder.assert_called_once_with("Test Reminder", "Work", "tomorrow")
        mock_print.assert_called_once_with("reminder added")

    @patch("builtins.print")
    def test_main_add_missing_title(self, mock_print):
        """Test main function with add action but missing title."""
        with patch.dict(os.environ, {"ACTION": "add"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.apple_reminders.executor import main
                main()

        assert excinfo.value.code == 1

    @patch("executors.apple_reminders.executor.complete_reminder")
    @patch("builtins.print")
    def test_main_complete_action(self, mock_print, mock_complete_reminder):
        """Test main function with complete action."""
        mock_complete_reminder.return_value = {"ok": True, "output": "reminder completed"}

        with patch.dict(os.environ, {"ACTION": "complete", "ID": "123"}):
            from executors.apple_reminders.executor import main
            main()

        mock_complete_reminder.assert_called_once_with("123")
        mock_print.assert_called_once_with("reminder completed")

    @patch("builtins.print")
    def test_main_complete_missing_id(self, mock_print):
        """Test main function with complete action but missing ID."""
        with patch.dict(os.environ, {"ACTION": "complete"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.apple_reminders.executor import main
                main()

        assert excinfo.value.code == 1

    @patch("builtins.print")
    def test_main_unknown_action(self, mock_print):
        """Test main function with unknown action."""
        with patch.dict(os.environ, {"ACTION": "unknown"}):
            with pytest.raises(SystemExit) as excinfo:
                from executors.apple_reminders.executor import main
                main()

        assert excinfo.value.code == 1

    @patch("executors.apple_reminders.executor.list_reminders")
    @patch("builtins.print")
    def test_main_default_action(self, mock_print, mock_list_reminders):
        """Test main function with no ACTION set (should default to list)."""
        mock_list_reminders.return_value = {"ok": True, "output": "default list"}

        with patch.dict(os.environ, {}, clear=True):
            from executors.apple_reminders.executor import main
            main()

        mock_list_reminders.assert_called_once_with("all")
        mock_print.assert_called_once_with("default list")