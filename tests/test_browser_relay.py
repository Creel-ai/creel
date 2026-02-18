"""Tests for the BrowserRelay service."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.browser import (
    BrowserRelay,
    ALLOWED_SCHEMES,
    CREEL_CONTAINER_LABEL,
    _reap_orphaned_containers,
    _start_chromium_container,
    _stop_container,
)


@pytest.fixture
def relay():
    """Create a BrowserRelay instance without starting Playwright."""
    r = BrowserRelay(max_sessions=3, session_timeout_minutes=10, blocked_domains=["evil.com"])
    return r


class TestURLValidation:
    """Test URL scheme and domain validation (allowlist: http/https only)."""

    def test_blocks_file_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("file:///etc/passwd")

    def test_blocks_javascript_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("javascript:alert(1)")

    def test_blocks_data_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("data:text/html,<h1>hi</h1>")

    def test_blocks_blob_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("blob:https://example.com/uuid")

    def test_blocks_ftp_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("ftp://internal.server/secret")

    def test_blocks_chrome_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("chrome://settings")

    def test_blocks_chrome_extension_scheme(self, relay):
        with pytest.raises(ValueError, match="not allowed"):
            relay._validate_url("chrome-extension://abc/popup.html")

    def test_blocks_configured_domain(self, relay):
        with pytest.raises(ValueError, match="blocked"):
            relay._validate_url("https://evil.com/page")

    def test_blocks_subdomain_of_blocked_domain(self, relay):
        with pytest.raises(ValueError, match="blocked"):
            relay._validate_url("https://sub.evil.com/page")

    def test_allows_https(self, relay):
        relay._validate_url("https://example.com")

    def test_allows_http(self, relay):
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

    @staticmethod
    def _make_page_mock(snapshot_text: str | None) -> MagicMock:
        """Create a page mock with locator().aria_snapshot() returning the given text."""
        page = MagicMock()
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(return_value=snapshot_text)
        page.locator.return_value = locator
        return page

    @pytest.mark.asyncio
    async def test_empty_snapshot(self, relay):
        page = self._make_page_mock(None)
        result = await relay._get_accessibility_tree(page)
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_string_snapshot(self, relay):
        page = self._make_page_mock("")
        result = await relay._get_accessibility_tree(page)
        assert result == []

    @pytest.mark.asyncio
    async def test_basic_tree(self, relay):
        snapshot = (
            '- heading "Hello World" [level=1]\n'
            '- link "Click me"\n'
            '- textbox "Search": query'
        )
        page = self._make_page_mock(snapshot)
        result = await relay._get_accessibility_tree(page)

        assert len(result) == 3
        roles = [n["role"] for n in result]
        assert "heading" in roles
        assert "link" in roles
        assert "textbox" in roles

        # Check heading attributes
        heading = next(n for n in result if n["role"] == "heading")
        assert heading["name"] == "Hello World"
        assert heading["level"] == 1

        # Check textbox value
        textbox = next(n for n in result if n["role"] == "textbox")
        assert textbox["name"] == "Search"
        assert textbox["value"] == "query"

    @pytest.mark.asyncio
    async def test_nested_indentation(self, relay):
        """Test that indentation is parsed as depth levels."""
        snapshot = (
            '- navigation "Main":\n'
            '  - link "Home"\n'
            '  - link "About"'
        )
        page = self._make_page_mock(snapshot)
        result = await relay._get_accessibility_tree(page)

        links = [n for n in result if n["role"] == "link"]
        assert len(links) == 2
        assert all(n.get("level") == 1 for n in links)

    @pytest.mark.asyncio
    async def test_truncation(self):
        """Test that content is truncated at max_content_chars."""
        relay = BrowserRelay(max_content_chars=50)
        snapshot = (
            '- heading "' + "A" * 30 + '" [level=1]\n'
            '- heading "' + "B" * 30 + '" [level=2]\n'
            '- heading "' + "C" * 30 + '" [level=3]'
        )
        page = self._make_page_mock(snapshot)
        result = await relay._get_accessibility_tree(page)
        # Should have stopped before including all nodes
        total_chars = sum(len(n.get("name", "")) for n in result)
        assert total_chars <= 80  # Some overshoot is OK due to per-line check

    @pytest.mark.asyncio
    async def test_selector_passed_to_locator(self, relay):
        """Test that a CSS selector is forwarded to page.locator()."""
        page = self._make_page_mock('- heading "Scoped" [level=2]')
        await relay._get_accessibility_tree(page, selector="#main")
        page.locator.assert_called_with("#main")

    @pytest.mark.asyncio
    async def test_no_selector_uses_body(self, relay):
        """Test that no selector defaults to body."""
        page = self._make_page_mock('- heading "All" [level=1]')
        await relay._get_accessibility_tree(page)
        page.locator.assert_called_with("body")

    @pytest.mark.asyncio
    async def test_aria_snapshot_exception_returns_empty(self, relay):
        """Test that an exception from aria_snapshot returns empty list."""
        page = MagicMock()
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(side_effect=Exception("timeout"))
        page.locator.return_value = locator
        result = await relay._get_accessibility_tree(page)
        assert result == []

    @pytest.mark.asyncio
    async def test_escaped_quotes_in_name(self, relay):
        """Test that names with escaped quotes are parsed correctly."""
        snapshot = '- button "Say \\"Hello\\""\n'
        page = self._make_page_mock(snapshot)
        result = await relay._get_accessibility_tree(page)
        assert len(result) == 1
        assert result[0]["name"] == 'Say "Hello"'


class TestCDPURLValidation:
    """Test CDP URL validation (localhost only)."""

    def test_allows_localhost(self, relay):
        relay._validate_cdp_url("http://localhost:9222")

    def test_allows_127_0_0_1(self, relay):
        relay._validate_cdp_url("http://127.0.0.1:9222")

    def test_allows_ipv6_loopback(self, relay):
        relay._validate_cdp_url("http://[::1]:9222")

    def test_blocks_remote_host(self, relay):
        with pytest.raises(ValueError, match="localhost"):
            relay._validate_cdp_url("http://192.168.1.100:9222")

    def test_blocks_cloud_metadata(self, relay):
        with pytest.raises(ValueError, match="localhost"):
            relay._validate_cdp_url("http://169.254.169.254/latest/meta-data")

    def test_blocks_internal_hostname(self, relay):
        with pytest.raises(ValueError, match="localhost"):
            relay._validate_cdp_url("http://internal-service:9222")


class TestPageHandlers:
    """Test page event handler installation."""

    def test_install_page_handlers(self, relay):
        """Test that dialog/popup/download handlers are registered."""
        page = MagicMock()
        relay._install_page_handlers(page)
        calls = [c[0][0] for c in page.on.call_args_list]
        assert "dialog" in calls
        assert "popup" in calls
        assert "download" in calls


class TestPostNavigationValidation:
    """Test that click/type_text validate the resulting URL."""

    @pytest.mark.asyncio
    async def test_click_blocks_navigation_to_bad_scheme(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        # After click, page ends up at file:// URL
        page.url = "file:///etc/passwd"

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        with pytest.raises(ValueError, match="not allowed"):
            await relay.click("s1", "a.malicious-link")

        # Should have tried to go back
        page.go_back.assert_called_once()

    @pytest.mark.asyncio
    async def test_click_allows_safe_navigation(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        page.url = "https://example.com/page2"

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.click("s1", "a#safe-link")
        assert result["ok"] is True
        assert result["url"] == "https://example.com/page2"

    @pytest.mark.asyncio
    async def test_type_blocks_navigation_to_blocked_domain(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        # After form submission, page navigates to blocked domain
        page.url = "https://evil.com/phishing"

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        with pytest.raises(ValueError, match="blocked"):
            await relay.type_text("s1", "input#search", "query")

        page.go_back.assert_called_once()

class TestNavigate:
    """Test the navigate method."""

    @pytest.mark.asyncio
    async def test_navigate_success(self, relay):
        from bridge.browser import BrowserSession

        page = AsyncMock()
        page.title.return_value = "Example"
        page.url = "https://example.com"
        # page.locator() is sync, so use MagicMock to avoid returning a coroutine
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(return_value='- heading "Hello" [level=1]')
        page.locator = MagicMock(return_value=locator)

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
        page.url = "https://example.com"

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.type_text("s1", "input#search", "hello world")

        assert result["ok"] is True
        assert result["url"] == "https://example.com"
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
        # First call: docker run, second call: docker port
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="abc123container\n", stderr=""),
            MagicMock(returncode=0, stdout="127.0.0.1:55432\n", stderr=""),
        ]

        container_id, port = _start_chromium_container()

        assert container_id == "abc123container"
        assert port == 55432
        assert mock_run.call_count == 2
        # Check docker run command
        run_cmd = mock_run.call_args_list[0][0][0]
        assert "docker" in run_cmd
        assert "--cap-drop=ALL" in run_cmd
        assert "--read-only" in run_cmd
        assert "--memory=512m" in run_cmd
        assert "-p" in run_cmd
        # Check docker port command
        port_cmd = mock_run.call_args_list[1][0][0]
        assert port_cmd == ["docker", "port", "abc123container", "9222"]

    @patch("bridge.browser.subprocess.run")
    def test_start_chromium_container_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="image not found"
        )

        with pytest.raises(RuntimeError, match="Failed to start"):
            _start_chromium_container()

    @patch("bridge.browser.subprocess.run")
    def test_start_chromium_container_port_query_failure(self, mock_run):
        """Container starts but port query fails — container is cleaned up."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="abc123container\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="no such container"),
        ]

        with pytest.raises(RuntimeError, match="Failed to get container port"):
            _start_chromium_container()

        # Should have tried to stop the container
        assert mock_run.call_count == 3  # run + port + stop
        stop_cmd = mock_run.call_args_list[2][0][0]
        assert stop_cmd == ["docker", "stop", "abc123container"]

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


class TestReapOrphanedContainers:
    """Test _reap_orphaned_containers function."""

    @patch("bridge.browser.subprocess.run")
    def test_reaps_orphaned_containers(self, mock_run):
        """Should find and stop all labeled containers."""
        mock_run.side_effect = [
            # docker ps -q --filter label=...
            MagicMock(returncode=0, stdout="aaa111\nbbb222\n", stderr=""),
            # docker stop aaa111
            MagicMock(returncode=0, stdout="", stderr=""),
            # docker stop bbb222
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        count = _reap_orphaned_containers()

        assert count == 2
        # First call: docker ps
        ps_cmd = mock_run.call_args_list[0][0][0]
        assert ps_cmd == ["docker", "ps", "-q", "--filter", f"label={CREEL_CONTAINER_LABEL}"]
        # Second call: docker stop aaa111
        assert mock_run.call_args_list[1][0][0] == ["docker", "stop", "aaa111"]
        # Third call: docker stop bbb222
        assert mock_run.call_args_list[2][0][0] == ["docker", "stop", "bbb222"]

    @patch("bridge.browser.subprocess.run")
    def test_no_orphans(self, mock_run):
        """Should return 0 when no containers are found."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        count = _reap_orphaned_containers()

        assert count == 0
        mock_run.assert_called_once()

    @patch("bridge.browser.subprocess.run")
    def test_docker_unavailable(self, mock_run):
        """Should return 0 when Docker is not installed."""
        mock_run.side_effect = FileNotFoundError("docker not found")

        count = _reap_orphaned_containers()

        assert count == 0

    @patch("bridge.browser.subprocess.run")
    def test_docker_ps_failure(self, mock_run):
        """Should return 0 when docker ps fails."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        count = _reap_orphaned_containers()

        assert count == 0

    @patch("bridge.browser.subprocess.run")
    def test_docker_timeout(self, mock_run):
        """Should return 0 when docker ps times out."""
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd="docker", timeout=10)

        count = _reap_orphaned_containers()

        assert count == 0


class TestContainerLabel:
    """Test that managed containers are created with the correct label."""

    @patch("bridge.browser.subprocess.run")
    def test_docker_run_includes_label(self, mock_run):
        """The docker run command should include --label with CREEL_CONTAINER_LABEL."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
            MagicMock(returncode=0, stdout="127.0.0.1:55432\n", stderr=""),
        ]

        _start_chromium_container()

        run_cmd = mock_run.call_args_list[0][0][0]
        # Find --label and the value that follows it
        label_idx = run_cmd.index("--label")
        assert run_cmd[label_idx + 1] == CREEL_CONTAINER_LABEL


class TestCleanupLoopDeadConnections:
    """Test that the cleanup loop detects dead browser connections."""

    @pytest.mark.asyncio
    async def test_cleanup_closes_dead_connections(self):
        """Sessions with disconnected browsers should be cleaned up."""
        from bridge.browser import BrowserSession

        relay = BrowserRelay(session_timeout_minutes=10)

        # Create a session with a dead browser (is_connected returns False)
        dead_browser = MagicMock()
        dead_browser.is_connected.return_value = False
        dead_browser.close = AsyncMock()

        relay._sessions["dead-1"] = BrowserSession(
            session_id="dead-1",
            mode="managed",
            browser=dead_browser,
            container_id="container-dead",
            last_used=time.time(),  # recently used, not idle
        )

        # Create a session with a live browser
        live_browser = MagicMock()
        live_browser.is_connected.return_value = True

        relay._sessions["live-1"] = BrowserSession(
            session_id="live-1",
            mode="managed",
            browser=live_browser,
            container_id="container-live",
            last_used=time.time(),
        )

        with patch("bridge.browser._stop_container"):
            # Run one iteration of the cleanup logic (bypass the sleep)
            now = time.time()
            to_close = []
            for sid, s in relay._sessions.items():
                if now - s.last_used > relay._session_timeout:
                    to_close.append(sid)
                elif s.browser and not s.browser.is_connected():
                    to_close.append(sid)

            for sid in to_close:
                await relay.close_session(sid)

        # Dead session should be removed, live session should remain
        assert "dead-1" not in relay._sessions
        assert "live-1" in relay._sessions


class TestGetSessionDeadBrowser:
    """Test that _get_session rejects dead browser sessions."""

    def test_rejects_dead_browser_connection(self, relay):
        """_get_session should raise ValueError for dead connections."""
        from bridge.browser import BrowserSession

        dead_browser = MagicMock()
        dead_browser.is_connected.return_value = False

        relay._sessions["dead-1"] = BrowserSession(
            session_id="dead-1",
            mode="managed",
            browser=dead_browser,
            container_id="container-dead",
        )

        # Need a running event loop for the async cleanup task scheduling
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with pytest.raises(ValueError, match="dead browser connection"):
                relay._get_session("dead-1")
        finally:
            # Clean up pending tasks
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def test_allows_connected_browser(self, relay):
        """_get_session should return the session if browser is connected."""
        from bridge.browser import BrowserSession

        live_browser = MagicMock()
        live_browser.is_connected.return_value = True

        session = BrowserSession(
            session_id="live-1",
            mode="managed",
            browser=live_browser,
        )
        relay._sessions["live-1"] = session

        result = relay._get_session("live-1")
        assert result is session

    def test_allows_session_without_browser(self, relay):
        """_get_session should work when browser is None (no is_connected check)."""
        from bridge.browser import BrowserSession

        session = BrowserSession(
            session_id="no-browser",
            mode="managed",
            browser=None,
        )
        relay._sessions["no-browser"] = session

        result = relay._get_session("no-browser")
        assert result is session


class TestStartReapsOrphans:
    """Test that BrowserRelay.start() calls _reap_orphaned_containers."""

    @pytest.mark.asyncio
    async def test_start_reaps_orphans(self):
        """start() should reap orphaned containers before initializing Playwright."""
        relay = BrowserRelay()

        pw_instance = AsyncMock()
        pw_context_manager = AsyncMock()
        pw_context_manager.start.return_value = pw_instance

        with patch("bridge.browser._reap_orphaned_containers", return_value=2) as mock_reap, \
             patch("playwright.async_api.async_playwright", return_value=pw_context_manager):
            await relay.start()

        mock_reap.assert_called_once()

        # Cleanup
        if relay._cleanup_task:
            relay._cleanup_task.cancel()
            try:
                await relay._cleanup_task
            except asyncio.CancelledError:
                pass
