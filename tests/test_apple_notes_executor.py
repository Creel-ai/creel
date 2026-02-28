"""Tests for the apple_notes executor."""

import os
from unittest.mock import MagicMock, patch

import pytest

from executors.apple_notes.executor import (
    call_bridge,
    create_note,
    list_notes,
    search_notes,
)


class TestBridgeClient:
    """Test the bridge client functionality in the apple_notes executor."""

    @patch("executors.apple_notes.executor.requests.post")
    def test_call_bridge_success(self, mock_post):
        """Test successful bridge call."""
        # Mock successful HTTP response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "success"}
        mock_post.return_value = mock_response

        # Mock environment variables
        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"},
        ):
            result = call_bridge("/notes/list")

        assert result["ok"] is True
        assert result["output"] == "success"

        # Verify correct HTTP request was made
        mock_post.assert_called_once_with(
            "http://localhost:8099/notes/list",
            json={},
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    @patch("executors.apple_notes.executor.requests.post")
    def test_call_bridge_with_data(self, mock_post):
        """Test bridge call with request data."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "success"}
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"},
        ):
            result = call_bridge("/notes/search", {"query": "test"})

        assert result["ok"] is True

        # Verify data was passed correctly
        mock_post.assert_called_once_with(
            "http://localhost:8099/notes/search",
            json={"query": "test"},
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def test_call_bridge_missing_url(self):
        """Test that missing BRIDGE_URL raises error."""
        with patch.dict(os.environ, {"BRIDGE_TOKEN": "test-token"}, clear=True):
            with pytest.raises(
                RuntimeError, match="BRIDGE_URL environment variable not set"
            ):
                call_bridge("/notes/list")

    def test_call_bridge_missing_token(self):
        """Test that missing BRIDGE_TOKEN raises error."""
        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099"}, clear=True
        ):
            with pytest.raises(
                RuntimeError, match="BRIDGE_TOKEN environment variable not set"
            ):
                call_bridge("/notes/list")

    @patch("executors.apple_notes.executor.requests.post")
    def test_call_bridge_http_error(self, mock_post):
        """Test handling of HTTP errors."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError("Connection failed")

        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"},
        ):
            with pytest.raises(RuntimeError, match="Bridge request failed"):
                call_bridge("/notes/list")

    @patch("executors.apple_notes.executor.requests.post")
    def test_call_bridge_api_error(self, mock_post):
        """Test handling of bridge API errors."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": False, "error": "Command failed"}
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"},
        ):
            with pytest.raises(RuntimeError, match="Bridge error: Command failed"):
                call_bridge("/notes/list")


class TestNotesOperations:
    """Test notes operations that call the bridge."""

    @patch("executors.apple_notes.executor.call_bridge")
    def test_list_notes(self, mock_call_bridge):
        """Test list_notes function."""
        mock_call_bridge.return_value = {"ok": True, "output": "note1\nnote2"}

        result = list_notes()

        assert result["ok"] is True
        assert result["output"] == "note1\nnote2"
        mock_call_bridge.assert_called_once_with("/notes/list", None)

    @patch("executors.apple_notes.executor.call_bridge")
    def test_list_notes_with_folder(self, mock_call_bridge):
        """Test list_notes function with folder filter."""
        mock_call_bridge.return_value = {"ok": True, "output": "filtered notes"}

        result = list_notes("work")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/notes/list", {"folder": "work"})

    @patch("executors.apple_notes.executor.call_bridge")
    def test_search_notes(self, mock_call_bridge):
        """Test search_notes function."""
        mock_call_bridge.return_value = {"ok": True, "output": "search results"}

        result = search_notes("test query")

        assert result["ok"] is True
        assert result["output"] == "search results"
        mock_call_bridge.assert_called_once_with(
            "/notes/search", {"query": "test query"}
        )

    @patch("executors.apple_notes.executor.call_bridge")
    def test_create_note_basic(self, mock_call_bridge):
        """Test create_note function with basic parameters."""
        mock_call_bridge.return_value = {"ok": True, "output": "note created"}

        result = create_note("Test Title", "Test Body")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/notes/create", {"title": "Test Title", "body": "Test Body"}
        )

    @patch("executors.apple_notes.executor.call_bridge")
    def test_create_note_with_folder(self, mock_call_bridge):
        """Test create_note function with folder."""
        mock_call_bridge.return_value = {"ok": True, "output": "note created"}

        result = create_note("Test Title", "Test Body", "work")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/notes/create",
            {"title": "Test Title", "body": "Test Body", "folder": "work"},
        )


class TestMainFunction:
    """Test the main executor function."""

    @patch("executors.apple_notes.executor.list_notes")
    @patch("builtins.print")
    def test_main_list_action(self, mock_print, mock_list_notes):
        """Test main function with list action."""
        mock_list_notes.return_value = {"ok": True, "output": "note1\nnote2"}

        with patch.dict(os.environ, {"ACTION": "list"}):
            from executors.apple_notes.executor import main

            main()

        mock_list_notes.assert_called_once_with(None)
        mock_print.assert_called_once_with("note1\nnote2")

    @patch("executors.apple_notes.executor.list_notes")
    @patch("builtins.print")
    def test_main_list_with_folder(self, mock_print, mock_list_notes):
        """Test main function with list action and folder."""
        mock_list_notes.return_value = {"ok": True, "output": "filtered notes"}

        with patch.dict(os.environ, {"ACTION": "list", "FOLDER": "work"}):
            from executors.apple_notes.executor import main

            main()

        mock_list_notes.assert_called_once_with("work")
        mock_print.assert_called_once_with("filtered notes")

    @patch("executors.apple_notes.executor.search_notes")
    @patch("builtins.print")
    def test_main_search_action(self, mock_print, mock_search_notes):
        """Test main function with search action."""
        mock_search_notes.return_value = {"ok": True, "output": "search results"}

        with patch.dict(os.environ, {"ACTION": "search", "QUERY": "test query"}):
            from executors.apple_notes.executor import main

            main()

        mock_search_notes.assert_called_once_with("test query")
        mock_print.assert_called_once_with("search results")

    @patch("builtins.print")
    def test_main_search_missing_query(self, mock_print):
        """Test main function with search action but missing query."""
        with patch.dict(os.environ, {"ACTION": "search"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.apple_notes.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.apple_notes.executor.create_note")
    @patch("builtins.print")
    def test_main_create_action(self, mock_print, mock_create_note):
        """Test main function with create action."""
        mock_create_note.return_value = {"ok": True, "output": "note created"}

        with patch.dict(
            os.environ,
            {
                "ACTION": "create",
                "TITLE": "Test Note",
                "BODY": "Note content",
                "FOLDER": "work",
            },
        ):
            from executors.apple_notes.executor import main

            main()

        mock_create_note.assert_called_once_with("Test Note", "Note content", "work")
        mock_print.assert_called_once_with("note created")

    @patch("builtins.print")
    def test_main_create_missing_title(self, mock_print):
        """Test main function with create action but missing title."""
        with patch.dict(os.environ, {"ACTION": "create"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.apple_notes.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("builtins.print")
    def test_main_read_action_not_implemented(self, mock_print):
        """Test main function with read action (not implemented)."""
        with patch.dict(os.environ, {"ACTION": "read"}):
            with pytest.raises(SystemExit) as excinfo:
                from executors.apple_notes.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("builtins.print")
    def test_main_unknown_action(self, mock_print):
        """Test main function with unknown action."""
        with patch.dict(os.environ, {"ACTION": "unknown"}):
            with pytest.raises(SystemExit) as excinfo:
                from executors.apple_notes.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.apple_notes.executor.list_notes")
    @patch("builtins.print")
    def test_main_default_action(self, mock_print, mock_list_notes):
        """Test main function with no ACTION set (should default to list)."""
        mock_list_notes.return_value = {"ok": True, "output": "default list"}

        with patch.dict(os.environ, {}, clear=True):
            from executors.apple_notes.executor import main

            main()

        mock_list_notes.assert_called_once_with(None)
        mock_print.assert_called_once_with("default list")
