"""Tests for the browser executor."""

from __future__ import annotations

import json
import os
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from executors.browser import executor


class TestCallBridge:
    """Test the bridge calling mechanism."""

    def test_missing_bridge_url(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BRIDGE_URL", None)
            os.environ.pop("BRIDGE_TOKEN", None)
            with pytest.raises(RuntimeError, match="BRIDGE_URL"):
                executor.call_bridge("/browser/connect", {})

    def test_missing_bridge_token(self):
        with patch.dict(os.environ, {"BRIDGE_URL": "http://localhost:8766"}, clear=True):
            os.environ.pop("BRIDGE_TOKEN", None)
            with pytest.raises(RuntimeError, match="BRIDGE_TOKEN"):
                executor.call_bridge("/browser/connect", {})

    @patch("executors.browser.executor.requests.post")
    def test_successful_call(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "session_id": "abc"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8766", "BRIDGE_TOKEN": "test-token"},
        ):
            result = executor.call_bridge("/browser/connect", {"mode": "managed"})

        assert result["ok"] is True
        assert result["session_id"] == "abc"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "http://localhost:8766/browser/connect"
        assert kwargs["json"] == {"mode": "managed"}
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"

    @patch("executors.browser.executor.requests.post")
    def test_bridge_error_response(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "error": "session limit"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8766", "BRIDGE_TOKEN": "test-token"},
        ):
            with pytest.raises(RuntimeError, match="session limit"):
                executor.call_bridge("/browser/connect", {})

    @patch("executors.browser.executor.requests.post")
    def test_request_exception(self, mock_post):
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError("refused")

        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8766", "BRIDGE_TOKEN": "test-token"},
        ):
            with pytest.raises(RuntimeError, match="Bridge request failed"):
                executor.call_bridge("/browser/connect", {})

    @patch("executors.browser.executor.requests.get")
    def test_get_method(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "sessions": []}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8766", "BRIDGE_TOKEN": "test-token"},
        ):
            result = executor.call_bridge("/browser/sessions", method="GET")

        assert result["ok"] is True
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert args[0] == "http://localhost:8766/browser/sessions"

    @patch("executors.browser.executor.requests.post")
    def test_post_no_body(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ,
            {"BRIDGE_URL": "http://localhost:8766", "BRIDGE_TOKEN": "test-token"},
        ):
            result = executor.call_bridge("/browser/connect")

        assert result["ok"] is True
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert "json" not in kwargs


class TestWrapperFunctions:
    """Test the individual wrapper functions."""

    @patch("executors.browser.executor.call_bridge")
    def test_connect(self, mock_bridge):
        mock_bridge.return_value = {"ok": True, "session_id": "s1"}
        result = executor.connect(mode="managed", headless=True)
        assert result["session_id"] == "s1"
        mock_bridge.assert_called_once_with(
            "/browser/connect", {"mode": "managed", "headless": True}, timeout=60
        )

    @patch("executors.browser.executor.call_bridge")
    def test_connect_relay_with_cdp_url(self, mock_bridge):
        mock_bridge.return_value = {"ok": True, "session_id": "s1"}
        executor.connect(mode="relay", cdp_url="http://localhost:9222")
        mock_bridge.assert_called_once_with(
            "/browser/connect",
            {"mode": "relay", "headless": True, "cdp_url": "http://localhost:9222"},
            timeout=60,
        )

    @patch("executors.browser.executor.call_bridge")
    def test_connect_native_mode(self, mock_bridge):
        mock_bridge.return_value = {"ok": True, "session_id": "s1"}
        result = executor.connect(mode="native")
        assert result["session_id"] == "s1"
        mock_bridge.assert_called_once_with(
            "/browser/connect", {"mode": "native", "headless": True}, timeout=60
        )

    @patch("executors.browser.executor.call_bridge")
    def test_navigate(self, mock_bridge):
        mock_bridge.return_value = {
            "ok": True,
            "title": "Example",
            "url": "https://example.com",
            "content": [],
        }
        result = executor.navigate("s1", "https://example.com")
        assert result["title"] == "Example"
        mock_bridge.assert_called_once_with(
            "/browser/navigate",
            {"session_id": "s1", "url": "https://example.com"},
            timeout=60,
        )

    @patch("executors.browser.executor.call_bridge")
    def test_get_content(self, mock_bridge):
        mock_bridge.return_value = {"ok": True, "content": []}
        executor.get_content("s1", selector="#main")
        mock_bridge.assert_called_once_with(
            "/browser/content", {"session_id": "s1", "selector": "#main"}
        )

    @patch("executors.browser.executor.call_bridge")
    def test_click(self, mock_bridge):
        mock_bridge.return_value = {"ok": True}
        executor.click("s1", "button#go")
        mock_bridge.assert_called_once_with(
            "/browser/click", {"session_id": "s1", "selector": "button#go"}
        )

    @patch("executors.browser.executor.call_bridge")
    def test_type_text(self, mock_bridge):
        mock_bridge.return_value = {"ok": True}
        executor.type_text("s1", "input#q", "hello")
        mock_bridge.assert_called_once_with(
            "/browser/type",
            {"session_id": "s1", "selector": "input#q", "text": "hello"},
        )

    @patch("executors.browser.executor.call_bridge")
    def test_screenshot(self, mock_bridge):
        mock_bridge.return_value = {"ok": True, "base64_image": "abc"}
        result = executor.screenshot("s1", full_page=True)
        assert result["base64_image"] == "abc"
        mock_bridge.assert_called_once_with(
            "/browser/screenshot", {"session_id": "s1", "full_page": True}
        )

    @patch("executors.browser.executor.call_bridge")
    def test_get_links(self, mock_bridge):
        mock_bridge.return_value = {"ok": True, "links": []}
        executor.get_links("s1")
        mock_bridge.assert_called_once_with("/browser/links", {"session_id": "s1"})

    @patch("executors.browser.executor.call_bridge")
    def test_close_session(self, mock_bridge):
        mock_bridge.return_value = {"ok": True}
        executor.close_session("s1")
        mock_bridge.assert_called_once_with("/browser/close", {"session_id": "s1"})

    @patch("executors.browser.executor.call_bridge")
    def test_sessions(self, mock_bridge):
        mock_bridge.return_value = {"ok": True, "sessions": []}
        result = executor.sessions()
        assert result["ok"] is True
        mock_bridge.assert_called_once_with("/browser/sessions", method="GET", timeout=10)


class TestMain:
    """Test the main() entry point with ACTION dispatch."""

    @patch("executors.browser.executor.connect")
    def test_main_connect(self, mock_connect):
        mock_connect.return_value = {"ok": True, "session_id": "s1"}

        import sys

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()

        with patch.dict(
            os.environ,
            {"ACTION": "connect", "MODE": "managed", "HEADLESS": "true"},
        ):
            executor.main()

        sys.stdout = old_stdout
        output = json.loads(captured.getvalue())
        assert output["session_id"] == "s1"

    @patch("executors.browser.executor.navigate")
    def test_main_navigate(self, mock_navigate):
        mock_navigate.return_value = {
            "ok": True,
            "title": "Test",
            "url": "https://test.com",
            "content": [],
        }

        import sys

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()

        with patch.dict(
            os.environ,
            {
                "ACTION": "navigate",
                "SESSION_ID": "s1",
                "URL": "https://test.com",
            },
        ):
            executor.main()

        sys.stdout = old_stdout
        output = json.loads(captured.getvalue())
        assert output["title"] == "Test"

    def test_main_navigate_missing_session_id(self):

        with patch.dict(os.environ, {"ACTION": "navigate", "URL": "https://test.com"}):
            os.environ.pop("SESSION_ID", None)
            with pytest.raises(SystemExit):
                executor.main()

    def test_main_navigate_missing_url(self):

        with patch.dict(os.environ, {"ACTION": "navigate", "SESSION_ID": "s1"}):
            os.environ.pop("URL", None)
            with pytest.raises(SystemExit):
                executor.main()

    @patch("executors.browser.executor.click")
    def test_main_click(self, mock_click):
        mock_click.return_value = {"ok": True, "url": "https://test.com"}

        import sys

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()

        with patch.dict(
            os.environ,
            {"ACTION": "click", "SESSION_ID": "s1", "SELECTOR": "button"},
        ):
            executor.main()

        sys.stdout = old_stdout
        output = json.loads(captured.getvalue())
        assert output["ok"] is True

    @patch("executors.browser.executor.close_session")
    def test_main_close(self, mock_close):
        mock_close.return_value = {"ok": True}

        import sys

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()

        with patch.dict(os.environ, {"ACTION": "close", "SESSION_ID": "s1"}):
            executor.main()

        sys.stdout = old_stdout
        output = json.loads(captured.getvalue())
        assert output["ok"] is True

    def test_main_unknown_action(self):
        with patch.dict(os.environ, {"ACTION": "unknown"}):
            with pytest.raises(SystemExit):
                executor.main()
