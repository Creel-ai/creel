"""Tests for the things executor."""

import os
from unittest.mock import MagicMock, patch

import pytest

from executors.things.executor import (
    add_item,
    call_bridge,
    inbox,
    projects,
    search,
    today,
    upcoming,
    update_item,
)


class TestBridgeClient:
    """Test the bridge client functionality in the things executor."""

    @patch("executors.things.executor.requests.post")
    def test_call_bridge_success(self, mock_post):
        """Test successful bridge call."""
        # Mock successful HTTP response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "success"}
        mock_post.return_value = mock_response

        # Mock environment variables
        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            result = call_bridge("/things/inbox")

        assert result["ok"] is True
        assert result["output"] == "success"

        # Verify correct HTTP request was made
        mock_post.assert_called_once_with(
            "http://localhost:8099/things/inbox",
            json={},
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
                call_bridge("/things/inbox")

    def test_call_bridge_missing_token(self):
        """Test that missing BRIDGE_TOKEN raises error."""
        with patch.dict(os.environ, {"BRIDGE_URL": "http://localhost:8099"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_TOKEN environment variable not set"):
                call_bridge("/things/inbox")

    @patch("executors.things.executor.requests.post")
    def test_call_bridge_api_error(self, mock_post):
        """Test handling of bridge API errors."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": False, "error": "Command failed"}
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            with pytest.raises(RuntimeError, match="Bridge error: Command failed"):
                call_bridge("/things/inbox")


class TestThingsOperations:
    """Test Things 3 operations that call the bridge."""

    @patch("executors.things.executor.call_bridge")
    def test_inbox_default(self, mock_call_bridge):
        """Test inbox function with default limit."""
        mock_call_bridge.return_value = {"ok": True, "output": "inbox items"}

        result = inbox()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/things/inbox", {"limit": 50})

    @patch("executors.things.executor.call_bridge")
    def test_inbox_custom_limit(self, mock_call_bridge):
        """Test inbox function with custom limit."""
        mock_call_bridge.return_value = {"ok": True, "output": "inbox items"}

        result = inbox(25)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/things/inbox", {"limit": 25})

    @patch("executors.things.executor.call_bridge")
    def test_today(self, mock_call_bridge):
        """Test today function."""
        mock_call_bridge.return_value = {"ok": True, "output": "today items"}

        result = today()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/things/today")

    @patch("executors.things.executor.call_bridge")
    def test_upcoming(self, mock_call_bridge):
        """Test upcoming function."""
        mock_call_bridge.return_value = {"ok": True, "output": "upcoming items"}

        result = upcoming()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/things/upcoming")

    @patch("executors.things.executor.call_bridge")
    def test_search(self, mock_call_bridge):
        """Test search function."""
        mock_call_bridge.return_value = {"ok": True, "output": "search results"}

        result = search("test query")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/things/search", {"query": "test query"})

    @patch("executors.things.executor.call_bridge")
    def test_projects(self, mock_call_bridge):
        """Test projects function."""
        mock_call_bridge.return_value = {"ok": True, "output": "projects"}

        result = projects()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/things/projects")

    @patch("executors.things.executor.call_bridge")
    def test_add_item_basic(self, mock_call_bridge):
        """Test add_item function with basic parameters."""
        mock_call_bridge.return_value = {"ok": True, "output": "item added"}

        result = add_item("Test Task")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/things/add", {"title": "Test Task"})

    @patch("executors.things.executor.call_bridge")
    def test_add_item_full(self, mock_call_bridge):
        """Test add_item function with all parameters."""
        mock_call_bridge.return_value = {"ok": True, "output": "item added"}

        result = add_item(
            "Test Task",
            notes="Task notes",
            tags="work,urgent",
            when="today",
            list_name="Work",
            heading="Section",
        )

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/things/add",
            {
                "title": "Test Task",
                "notes": "Task notes",
                "tags": "work,urgent",
                "when": "today",
                "list": "Work",
                "heading": "Section",
            },
        )

    @patch("executors.things.executor.call_bridge")
    def test_update_item(self, mock_call_bridge):
        """Test update_item function."""
        mock_call_bridge.return_value = {"ok": True, "output": "item updated"}

        result = update_item(
            "task-123", completed=True, title="Updated Task", notes="Updated notes"
        )

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/things/update",
            {
                "id": "task-123",
                "completed": True,
                "title": "Updated Task",
                "notes": "Updated notes",
            },
        )


class TestMainFunction:
    """Test the main executor function."""

    @patch("executors.things.executor.inbox")
    @patch("builtins.print")
    def test_main_inbox_action(self, mock_print, mock_inbox):
        """Test main function with inbox action."""
        mock_inbox.return_value = {"ok": True, "output": "inbox items"}

        with patch.dict(os.environ, {"ACTION": "inbox"}):
            from executors.things.executor import main

            main()

        mock_inbox.assert_called_once_with(50)
        mock_print.assert_called_once_with("inbox items")

    @patch("executors.things.executor.inbox")
    @patch("builtins.print")
    def test_main_inbox_custom_limit(self, mock_print, mock_inbox):
        """Test main function with inbox action and custom limit."""
        mock_inbox.return_value = {"ok": True, "output": "inbox items"}

        with patch.dict(os.environ, {"ACTION": "inbox", "LIMIT": "25"}):
            from executors.things.executor import main

            main()

        mock_inbox.assert_called_once_with(25)
        mock_print.assert_called_once_with("inbox items")

    @patch("executors.things.executor.today")
    @patch("builtins.print")
    def test_main_today_action(self, mock_print, mock_today):
        """Test main function with today action."""
        mock_today.return_value = {"ok": True, "output": "today items"}

        with patch.dict(os.environ, {"ACTION": "today"}):
            from executors.things.executor import main

            main()

        mock_today.assert_called_once()
        mock_print.assert_called_once_with("today items")

    @patch("executors.things.executor.upcoming")
    @patch("builtins.print")
    def test_main_upcoming_action(self, mock_print, mock_upcoming):
        """Test main function with upcoming action."""
        mock_upcoming.return_value = {"ok": True, "output": "upcoming items"}

        with patch.dict(os.environ, {"ACTION": "upcoming"}):
            from executors.things.executor import main

            main()

        mock_upcoming.assert_called_once()
        mock_print.assert_called_once_with("upcoming items")

    @patch("executors.things.executor.search")
    @patch("builtins.print")
    def test_main_search_action(self, mock_print, mock_search):
        """Test main function with search action."""
        mock_search.return_value = {"ok": True, "output": "search results"}

        with patch.dict(os.environ, {"ACTION": "search", "QUERY": "test query"}):
            from executors.things.executor import main

            main()

        mock_search.assert_called_once_with("test query")
        mock_print.assert_called_once_with("search results")

    @patch("builtins.print")
    def test_main_search_missing_query(self, mock_print):
        """Test main function with search action but missing query."""
        with patch.dict(os.environ, {"ACTION": "search"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.things.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.things.executor.projects")
    @patch("builtins.print")
    def test_main_projects_action(self, mock_print, mock_projects):
        """Test main function with projects action."""
        mock_projects.return_value = {"ok": True, "output": "projects"}

        with patch.dict(os.environ, {"ACTION": "projects"}):
            from executors.things.executor import main

            main()

        mock_projects.assert_called_once()
        mock_print.assert_called_once_with("projects")

    @patch("executors.things.executor.add_item")
    @patch("builtins.print")
    def test_main_add_action(self, mock_print, mock_add_item):
        """Test main function with add action."""
        mock_add_item.return_value = {"ok": True, "output": "item added"}

        with patch.dict(
            os.environ,
            {
                "ACTION": "add",
                "TITLE": "Test Task",
                "NOTES": "Task notes",
                "TAGS": "work,urgent",
                "WHEN": "today",
                "LIST": "Work",
                "HEADING": "Section",
            },
        ):
            from executors.things.executor import main

            main()

        mock_add_item.assert_called_once_with(
            "Test Task", "Task notes", "work,urgent", "today", "Work", "Section"
        )
        mock_print.assert_called_once_with("item added")

    @patch("builtins.print")
    def test_main_add_missing_title(self, mock_print):
        """Test main function with add action but missing title."""
        with patch.dict(os.environ, {"ACTION": "add"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.things.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.things.executor.update_item")
    @patch("builtins.print")
    def test_main_update_action(self, mock_print, mock_update_item):
        """Test main function with update action."""
        mock_update_item.return_value = {"ok": True, "output": "item updated"}

        with patch.dict(
            os.environ,
            {
                "ACTION": "update",
                "ID": "task-123",
                "COMPLETED": "true",
                "TITLE": "Updated Task",
                "NOTES": "Updated notes",
            },
        ):
            from executors.things.executor import main

            main()

        mock_update_item.assert_called_once_with(
            "task-123", True, "Updated Task", "Updated notes", None
        )
        mock_print.assert_called_once_with("item updated")

    @patch("builtins.print")
    def test_main_update_missing_id(self, mock_print):
        """Test main function with update action but missing ID."""
        with patch.dict(os.environ, {"ACTION": "update"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.things.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("builtins.print")
    def test_main_unknown_action(self, mock_print):
        """Test main function with unknown action."""
        with patch.dict(os.environ, {"ACTION": "unknown"}):
            with pytest.raises(SystemExit) as excinfo:
                from executors.things.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.things.executor.inbox")
    @patch("builtins.print")
    def test_main_default_action(self, mock_print, mock_inbox):
        """Test main function with no ACTION set (should default to inbox)."""
        mock_inbox.return_value = {"ok": True, "output": "default inbox"}

        with patch.dict(os.environ, {}, clear=True):
            from executors.things.executor import main

            main()

        mock_inbox.assert_called_once_with(50)
        mock_print.assert_called_once_with("default inbox")
