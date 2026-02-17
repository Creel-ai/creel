"""Tests for the browser bridge endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bridge.server import app


@pytest.fixture
def client():
    """Test client for the bridge server."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def scoped_tokens():
    """Mock scoped tokens including BROWSER scope."""
    tokens = {
        "NOTES": "test-notes-token-123",
        "REMINDERS": "test-reminders-token-123",
        "THINGS": "test-things-token-123",
        "IMESSAGE": "test-imessage-token-123",
        "BROWSER": "test-browser-token-123",
    }
    with patch("bridge.server.SCOPED_TOKENS", tokens):
        yield tokens


@pytest.fixture
def browser_auth_headers(scoped_tokens):
    """Authentication headers for browser endpoints."""
    return {"Authorization": f"Bearer {scoped_tokens['BROWSER']}"}


@pytest.fixture
def mock_relay():
    """Mock BrowserRelay for testing endpoints."""
    relay = AsyncMock()
    # list_sessions is sync, so use a regular MagicMock for it
    relay.list_sessions = MagicMock(return_value=[])
    return relay


class TestBrowserAuth:
    """Test browser endpoint authentication."""

    def test_missing_auth_token(self, client):
        """Test that requests without auth token are rejected."""
        with patch.object(app.state, "browser_relay", AsyncMock(), create=True):
            response = client.post("/browser/connect", json={"mode": "managed"})
        assert response.status_code in (401, 403)

    def test_wrong_scope_token(self, client, scoped_tokens):
        """Test that non-browser tokens are rejected."""
        headers = {"Authorization": f"Bearer {scoped_tokens['NOTES']}"}
        with patch.object(app.state, "browser_relay", AsyncMock(), create=True):
            response = client.post(
                "/browser/connect", json={"mode": "managed"}, headers=headers
            )
        assert response.status_code == 401


class TestBrowserConnect:
    """Test browser connect endpoint."""

    def test_connect_managed(self, client, browser_auth_headers, mock_relay):
        mock_relay.create_managed.return_value = "session-123"

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/connect",
                json={"mode": "managed", "headless": True},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["session_id"] == "session-123"
        mock_relay.create_managed.assert_called_once_with(headless=True)

    def test_connect_relay(self, client, browser_auth_headers, mock_relay):
        mock_relay.connect_relay.return_value = "session-456"

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/connect",
                json={"mode": "relay", "cdp_url": "http://localhost:9222"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["session_id"] == "session-456"
        mock_relay.connect_relay.assert_called_once_with("http://localhost:9222")

    def test_connect_relay_missing_cdp_url(self, client, browser_auth_headers, mock_relay):
        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/connect",
                json={"mode": "relay"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 400

    def test_connect_invalid_mode(self, client, browser_auth_headers, mock_relay):
        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/connect",
                json={"mode": "invalid"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 400


class TestBrowserNavigate:
    """Test browser navigate endpoint."""

    def test_navigate_success(self, client, browser_auth_headers, mock_relay):
        mock_relay.navigate.return_value = {
            "title": "Example",
            "url": "https://example.com",
            "content": [{"role": "heading", "name": "Hello"}],
        }

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/navigate",
                json={"session_id": "s1", "url": "https://example.com"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["title"] == "Example"
        assert len(result["content"]) == 1

    def test_navigate_invalid_url(self, client, browser_auth_headers, mock_relay):
        mock_relay.navigate.side_effect = ValueError("URL scheme 'javascript' is not allowed")

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/navigate",
                json={"session_id": "s1", "url": "javascript:alert(1)"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert "not allowed" in result["error"]


class TestBrowserContent:
    """Test browser content endpoint."""

    def test_get_content(self, client, browser_auth_headers, mock_relay):
        mock_relay.get_content.return_value = {
            "title": "Page",
            "url": "https://example.com",
            "content": [{"role": "heading", "name": "Title"}],
        }

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/content",
                json={"session_id": "s1"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["title"] == "Page"

    def test_get_content_with_selector(self, client, browser_auth_headers, mock_relay):
        mock_relay.get_content.return_value = {
            "title": "Page",
            "url": "https://example.com",
            "content": [],
        }

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/content",
                json={"session_id": "s1", "selector": "#main"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        mock_relay.get_content.assert_called_once_with("s1", "#main")


class TestBrowserClick:
    """Test browser click endpoint."""

    def test_click_success(self, client, browser_auth_headers, mock_relay):
        mock_relay.click.return_value = {
            "ok": True,
            "url": "https://example.com/clicked",
        }

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/click",
                json={"session_id": "s1", "selector": "button#go"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        mock_relay.click.assert_called_once_with("s1", "button#go")


class TestBrowserType:
    """Test browser type endpoint."""

    def test_type_success(self, client, browser_auth_headers, mock_relay):
        mock_relay.type_text.return_value = {"ok": True}

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/type",
                json={
                    "session_id": "s1",
                    "selector": "input#q",
                    "text": "search query",
                },
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        mock_relay.type_text.assert_called_once_with("s1", "input#q", "search query")


class TestBrowserScreenshot:
    """Test browser screenshot endpoint."""

    def test_screenshot_success(self, client, browser_auth_headers, mock_relay):
        mock_relay.screenshot.return_value = {
            "base64_image": "iVBOR...",
            "url": "https://example.com",
        }

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/screenshot",
                json={"session_id": "s1", "full_page": True},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert "base64_image" in result
        mock_relay.screenshot.assert_called_once_with("s1", True)


class TestBrowserLinks:
    """Test browser links endpoint."""

    def test_get_links(self, client, browser_auth_headers, mock_relay):
        mock_relay.get_links.return_value = [
            {"text": "Home", "href": "https://example.com/"},
            {"text": "About", "href": "https://example.com/about"},
        ]

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/links",
                json={"session_id": "s1"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert len(result["links"]) == 2


class TestBrowserClose:
    """Test browser close endpoint."""

    def test_close_session(self, client, browser_auth_headers, mock_relay):
        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.post(
                "/browser/close",
                json={"session_id": "s1"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        mock_relay.close_session.assert_called_once_with("s1")


class TestBrowserSessions:
    """Test browser sessions listing endpoint."""

    def test_list_sessions(self, client, browser_auth_headers, mock_relay):
        mock_relay.list_sessions.return_value = [
            {"session_id": "s1", "mode": "managed", "last_used": 100.0, "url": "https://example.com"},
        ]

        with patch.object(app.state, "browser_relay", mock_relay, create=True):
            response = client.get(
                "/browser/sessions",
                headers=browser_auth_headers,
            )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert len(result["sessions"]) == 1


class TestBrowserRelayUnavailable:
    """Test behavior when browser relay is not available."""

    def test_connect_when_relay_unavailable(self, client, browser_auth_headers):
        with patch.object(app.state, "browser_relay", None, create=True):
            response = client.post(
                "/browser/connect",
                json={"mode": "managed"},
                headers=browser_auth_headers,
            )

        assert response.status_code == 503


class TestCrossScopeAccess:
    """Test that browser tokens can't access other endpoints and vice versa."""

    def test_browser_token_cant_access_notes(self, client, scoped_tokens):
        headers = {"Authorization": f"Bearer {scoped_tokens['BROWSER']}"}
        response = client.post("/notes/list", headers=headers)
        assert response.status_code == 401

    def test_notes_token_cant_access_browser(self, client, scoped_tokens):
        headers = {"Authorization": f"Bearer {scoped_tokens['NOTES']}"}
        with patch.object(app.state, "browser_relay", AsyncMock(), create=True):
            response = client.post(
                "/browser/connect",
                json={"mode": "managed"},
                headers=headers,
            )
        assert response.status_code == 401
