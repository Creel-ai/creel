"""Tests for the BrowserRelay service."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.browser import BrowserRelay, BLOCKED_SCHEMES, _start_chromium_container, _stop_container


@pytest.fixture
def relay():
    """Create a BrowserRelay instance without starting Playwright."""
    r = BrowserRelay(max_sessions=3, session_timeout_minutes=10, blocked_domains=["evil.com"])
    return r


class TestURLValidation:
    """Test URL scheme and domain validation."""

    def test_blocks_file_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("file:///etc/passwd")

    def test_blocks_javascript_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("javascript:alert(1)")

    def test_blocks_data_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("data:text/html,<h1>hi</h1>")

    def test_blocks_configured_domain(self, relay):
        with pytest.raises(ValueError, match="blocked"):
            relay._validate_url("https://evil.com/page")

    def test_blocks_subdomain_of_blocked_domain(self, relay):
        with pytest.raises(ValueError, match="blocked"):
            relay._validate_url("https://sub.evil.com/page")

    def test_allows_https(self, relay):
        # Should not raise
        relay._validate_url("https://example.com")

    def test_allows_http(self, relay):
        # Should not raise
        relay._validate_url("http://example.com")

    def test_rejects_missing_scheme(self, relay):
        with pytest.raises(ValueError, match="scheme"):
            relay._validate_url("example.com")


class TestSessionManagement:
    """Test session creation and lifecycle."""

    def test_session_limit_enforced(self, relay):
        """Test that exceeding max_sessions raises."""
        from bridge.browser import BrowserSession

        # Fill up sessions
        for i in range(3):
            relay._sessions[f"session-{i}"] = BrowserSession(
                session_id=f"session-{i}", mode="managed"
            )

        with pytest.raises(RuntimeError, match="Maximum sessions"):
            relay._check_session_limit()

    def test_get_session_found(self, relay):
        from bridge.browser import BrowserSession

        session = BrowserSession(session_id="test-123", mode="relay")
        relay._sessions["test-123"] = session

        result = relay._get_session("test-123")
        assert result is session

    def test_get_session_not_found(self, relay):
        with pytest.raises(ValueError, match="Unknown session"):
            relay._get_session("nonexistent")

    def test_list_sessions(self, relay):
        from bridge.browser import BrowserSession

        page_mock = MagicMock()
        page_mock.url = "https://example.com"

        relay._sessions["s1"] = BrowserSession(
            session_id="s1", mode="managed", page=page_mock, last_used=100.0
        )
        relay._sessions["s2"] = BrowserSession(
            session_id="s2", mode="relay", page=page_mock, last_used=200.0
        )

        sessions = relay.list_sessions()
        assert len(sessions) == 2
        assert sessions[0]["session_id"] == "s1"
        assert sessions[0]["mode"] == "managed"
        assert sessions[1]["session_id"] == "s2"
        assert sessions[1]["mode"] == "relay"

    @pytest.mark.asyncio
    async def test_close_session_managed_stops_container(self, relay):
        """Test that closing a managed session stops its Docker container."""
        from bridge.browser import BrowserSession

        browser_mock = AsyncMock()
        session = BrowserSession(
            session_id="managed-1",
            mode="managed",
            browser=browser_mock,
            container_id="abc123",
        )
        relay._sessions["managed-1"] = session

        with patch("bridge.browser._stop_container") as mock_stop:
            await relay.close_session("managed-1")
            mock_stop.assert_called_once_with("abc123")

        assert "managed-1" not in relay._sessions
        browser_mock.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_session_relay_no_container_stop(self, relay):
        """Test that closing a relay session does not try to stop a container."""
        from bridge.browser import BrowserSession

        browser_mock = AsyncMock()
        session = BrowserSession(
            session_id="relay-1",
            mode="relay",
            browser=browser_mock,
        )
        relay._sessions["relay-1"] = session

        with patch("bridge.browser._stop_container") as mock_stop:
            await relay.close_session("relay-1")
            mock_stop.assert_not_called()

        assert "relay-1" not in relay._sessions

    @pytest.mark.asyncio
    async def test_close_nonexistent_session(self, relay):
        """Closing a nonexistent session should not raise."""
        await relay.close_session("nonexistent")


class TestAccessibilityTree:
    """Test accessibility tree extraction."""

    @pytest.mark.asyncio
    async def test_empty_snapshot(self, relay):
        page = AsyncMock()
        page.accessibility.snapshot.return_value = None
        result = await relay._get_accessibility_tree(page)
        assert result == []

    @pytest.mark.asyncio
    async def test_basic_tree(self, relay):
        page = AsyncMock()
        page.accessibility.snapshot.return_value = {
            "role": "WebArea",
            "name": "Test Page",
            "children": [
                {"role": "heading", "name": "Hello World", "children": []},
                {"role": "link", "name": "Click me", "children": []},
                {
                    "role": "textbox",
                    "name": "Search",
                    "value": "query",
                    "children": [],
                },
            ],
        }
        result = await relay._get_accessibility_tree(page)

        assert len(result) >= 3
        roles = [n["role"] for n in result]
        assert "heading" in roles
        assert "link" in roles
        assert "textbox" in roles

    @pytest.mark.asyncio
    async def test_truncation(self):
        """Test that content is truncated at max_content_chars."""
        relay = BrowserRelay(max_content_chars=50)
        page = AsyncMock()
        page.accessibility.snapshot.return_value = {
            "role": "WebArea",
            "name": "Page",
            "children": [
                {"role": "heading", "name": "A" * 30, "children": []},
                {"role": "heading", "name": "B" * 30, "children": []},
                {"role": "heading", "name": "C" * 30, "children": []},
            ],
        }
        result = await relay._get_accessibility_tree(page)
        # Should have stopped before including all nodes
        total_chars = sum(len(n.get("name", "")) for n in result)
        assert total_chars <= 80  # Some overshoot is OK due to walking


class TestNavigate:
    """Test the navigate method."""

    @pytest.mark.asyncio
    async def test_navigate_success(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        page.title.return_value = "Example"
        page.url = "https://example.com"
        page.accessibility.snapshot.return_value = {
            "role": "WebArea",
            "name": "Example",
            "children": [
                {"role": "heading", "name": "Hello", "children": []},
            ],
        }

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.navigate("s1", "https://example.com")

        assert result["title"] == "Example"
        assert result["url"] == "https://example.com"
        assert len(result["content"]) >= 1
        page.goto.assert_called_once_with("https://example.com", wait_until="domcontentloaded")

    @pytest.mark.asyncio
    async def test_navigate_blocked_url(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        with pytest.raises(ValueError, match="not allowed"):
            await relay.navigate("s1", "javascript:alert(1)")


class TestClick:
    """Test the click method."""

    @pytest.mark.asyncio
    async def test_click_success(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        page.url = "https://example.com/after-click"

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.click("s1", "button#submit")

        assert result["ok"] is True
        assert result["url"] == "https://example.com/after-click"
        page.click.assert_called_once_with("button#submit")


class TestTypeText:
    """Test the type_text method."""

    @pytest.mark.asyncio
    async def test_type_success(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.type_text("s1", "input#search", "hello world")

        assert result["ok"] is True
        page.fill.assert_called_once_with("input#search", "hello world")


class TestScreenshot:
    """Test the screenshot method."""

    @pytest.mark.asyncio
    async def test_screenshot_success(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        page.url = "https://example.com"
        page.screenshot.return_value = b"\x89PNG\r\n\x1a\n"  # Minimal PNG header

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.screenshot("s1", full_page=True)

        assert "base64_image" in result
        assert result["url"] == "https://example.com"
        page.screenshot.assert_called_once_with(full_page=True)


class TestGetLinks:
    """Test the get_links method."""

    @pytest.mark.asyncio
    async def test_get_links_success(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        page.evaluate.return_value = [
            {"text": "Home", "href": "https://example.com/"},
            {"text": "About", "href": "https://example.com/about"},
        ]

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.get_links("s1")

        assert len(result) == 2
        assert result[0]["text"] == "Home"
        assert result[1]["href"] == "https://example.com/about"


class TestContainerManagement:
    """Test Docker container lifecycle functions."""

    @patch("bridge.browser.subprocess.run")
    def test_start_chromium_container(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="abc123container\n", stderr=""
        )

        container_id = _start_chromium_container(9222)

        assert container_id == "abc123container"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "docker" in cmd
        assert "--cap-drop=ALL" in cmd
        assert "--read-only" in cmd
        assert "--memory=512m" in cmd
        assert "-p" in cmd

    @patch("bridge.browser.subprocess.run")
    def test_start_chromium_container_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="image not found"
        )

        with pytest.raises(RuntimeError, match="Failed to start"):
            _start_chromium_container(9222)

    @patch("bridge.browser.subprocess.run")
    def test_stop_container(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        _stop_container("abc123")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "stop", "abc123"]


class TestStartStop:
    """Test BrowserRelay start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_stop_cleans_all_sessions(self):
        """Test that stop() closes all sessions and containers."""
        relay = BrowserRelay()

        # Mock playwright
        pw_mock = AsyncMock()
        relay._playwright = pw_mock
        relay._cleanup_task = asyncio.create_task(asyncio.sleep(999))

        from bridge.browser import BrowserSession

        browser1 = AsyncMock()
        browser2 = AsyncMock()

        relay._sessions["s1"] = BrowserSession(
            session_id="s1",
            mode="managed",
            browser=browser1,
            container_id="container1",
        )
        relay._sessions["s2"] = BrowserSession(
            session_id="s2",
            mode="relay",
            browser=browser2,
        )

        with patch("bridge.browser._stop_container") as mock_stop:
            await relay.stop()
            # Only managed session should stop container
            mock_stop.assert_called_once_with("container1")

        assert len(relay._sessions) == 0
        pw_mock.stop.assert_called_once()
