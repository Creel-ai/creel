"""Tests for the apple_photos executor."""

import os
from unittest.mock import MagicMock, patch

import pytest

from executors.apple_photos.executor import call_bridge, recent_photos, search_photos


class TestBridgeClient:
    """Test the bridge client functionality in the apple_photos executor."""

    @patch("executors.apple_photos.executor.requests.post")
    def test_call_bridge_success(self, mock_post):
        """Test successful bridge call."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "[]"}
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            result = call_bridge("/photos/recent")

        assert result["ok"] is True
        assert result["output"] == "[]"

        mock_post.assert_called_once_with(
            "http://localhost:8099/photos/recent",
            json={},
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    @patch("executors.apple_photos.executor.requests.post")
    def test_call_bridge_with_data(self, mock_post):
        """Test bridge call with request data."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "[]"}
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            result = call_bridge("/photos/search", {"keyword": "beach"})

        assert result["ok"] is True

        mock_post.assert_called_once_with(
            "http://localhost:8099/photos/search",
            json={"keyword": "beach"},
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
                call_bridge("/photos/recent")

    def test_call_bridge_missing_token(self):
        """Test that missing BRIDGE_TOKEN raises error."""
        with patch.dict(os.environ, {"BRIDGE_URL": "http://localhost:8099"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_TOKEN environment variable not set"):
                call_bridge("/photos/recent")

    @patch("executors.apple_photos.executor.requests.post")
    def test_call_bridge_http_error(self, mock_post):
        """Test handling of HTTP errors."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError("Connection failed")

        with patch.dict(
            os.environ, {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "test-token"}
        ):
            with pytest.raises(RuntimeError, match="Bridge request failed"):
                call_bridge("/photos/recent")

    @patch("executors.apple_photos.executor.requests.post")
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
                call_bridge("/photos/recent")


class TestPhotosOperations:
    """Test photos operations that call the bridge."""

    @patch("executors.apple_photos.executor.call_bridge")
    def test_recent_photos_default(self, mock_call_bridge):
        """Test recent_photos with default count."""
        mock_call_bridge.return_value = {"ok": True, "output": "[]"}

        result = recent_photos()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/photos/recent", {"count": 10})

    @patch("executors.apple_photos.executor.call_bridge")
    def test_recent_photos_custom_count(self, mock_call_bridge):
        """Test recent_photos with custom count."""
        mock_call_bridge.return_value = {"ok": True, "output": "[]"}

        result = recent_photos(count=5)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/photos/recent", {"count": 5})

    @patch("executors.apple_photos.executor.call_bridge")
    def test_search_photos_keyword(self, mock_call_bridge):
        """Test search_photos with keyword."""
        mock_call_bridge.return_value = {"ok": True, "output": "[]"}

        result = search_photos(keyword="beach")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/photos/search", {"count": 20, "keyword": "beach"}
        )

    @patch("executors.apple_photos.executor.call_bridge")
    def test_search_photos_with_dates(self, mock_call_bridge):
        """Test search_photos with date range."""
        mock_call_bridge.return_value = {"ok": True, "output": "[]"}

        result = search_photos(keyword="trip", date_from="2025-01-01", date_to="2025-12-31")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/photos/search",
            {"count": 20, "keyword": "trip", "date_from": "2025-01-01", "date_to": "2025-12-31"},
        )

    @patch("executors.apple_photos.executor.call_bridge")
    def test_search_photos_no_keyword(self, mock_call_bridge):
        """Test search_photos without keyword."""
        mock_call_bridge.return_value = {"ok": True, "output": "[]"}

        result = search_photos(count=5)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/photos/search", {"count": 5})


class TestMainFunction:
    """Test the main executor function."""

    @patch("executors.apple_photos.executor.recent_photos")
    @patch("builtins.print")
    def test_main_recent_action(self, mock_print, mock_recent):
        """Test main function with recent action."""
        mock_recent.return_value = {"ok": True, "output": "[]"}

        with patch.dict(os.environ, {"ACTION": "recent"}):
            from executors.apple_photos.executor import main

            main()

        mock_recent.assert_called_once_with(10)
        mock_print.assert_called_once_with("[]")

    @patch("executors.apple_photos.executor.recent_photos")
    @patch("builtins.print")
    def test_main_recent_custom_count(self, mock_print, mock_recent):
        """Test main function with recent action and custom count."""
        mock_recent.return_value = {"ok": True, "output": "[]"}

        with patch.dict(os.environ, {"ACTION": "recent", "COUNT": "5"}):
            from executors.apple_photos.executor import main

            main()

        mock_recent.assert_called_once_with(5)

    @patch("executors.apple_photos.executor.search_photos")
    @patch("builtins.print")
    def test_main_search_action(self, mock_print, mock_search):
        """Test main function with search action."""
        mock_search.return_value = {"ok": True, "output": "[]"}

        with patch.dict(os.environ, {"ACTION": "search", "KEYWORD": "beach"}):
            from executors.apple_photos.executor import main

            main()

        mock_search.assert_called_once_with("beach", None, None, 20)
        mock_print.assert_called_once_with("[]")

    @patch("builtins.print")
    def test_main_unknown_action(self, mock_print):
        """Test main function with unknown action."""
        with patch.dict(os.environ, {"ACTION": "unknown"}):
            with pytest.raises(SystemExit) as excinfo:
                from executors.apple_photos.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.apple_photos.executor.recent_photos")
    @patch("builtins.print")
    def test_main_default_action(self, mock_print, mock_recent):
        """Test main function with no ACTION set (should default to recent)."""
        mock_recent.return_value = {"ok": True, "output": "[]"}

        with patch.dict(os.environ, {}, clear=True):
            from executors.apple_photos.executor import main

            main()

        mock_recent.assert_called_once_with(10)


class TestRegisterSkill:
    """Test skill registration."""

    def test_register_skill_returns_meta_and_execute(self):
        """Test that register_skill returns correct metadata."""
        from executors.apple_photos.executor import register_skill

        meta, execute = register_skill()

        assert meta.id == "apple_photos"
        assert meta.label == "Apple Photos"
        assert meta.needs_bridge is True
        assert meta.needs_network is True
        assert meta.bridge_scope == "PHOTOS"
        assert meta.platform == "darwin"
        assert len(meta.tools) == 2

        tool_names = {t.name for t in meta.tools}
        assert tool_names == {"recent_photos", "search_photos"}

    def test_register_skill_execute_callable(self):
        """Test that the execute function is callable."""
        from executors.apple_photos.executor import register_skill

        _, execute = register_skill()
        assert callable(execute)
