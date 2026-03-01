"""Tests for browser resilience: timeouts, error recovery, resource blocking."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.browser import (
    BLOCKED_RESOURCE_TYPES,
    BrowserRelay,
    BrowserSession,
    SessionDead,
    _start_chromium_container,
)


@pytest.fixture
def relay():
    """Create a BrowserRelay with short timeouts for testing."""
    return BrowserRelay(
        max_sessions=3,
        session_timeout_minutes=10,
        blocked_domains=["evil.com"],
        navigate_timeout_ms=5000,
        snapshot_timeout_ms=2000,
        block_heavy_resources=True,
    )


@pytest.fixture
def relay_no_blocking():
    """Create a BrowserRelay with resource blocking disabled."""
    return BrowserRelay(
        max_sessions=3,
        block_heavy_resources=False,
    )


class TestNavigateTimeout:
    """Test navigate timeout behaviour."""

    @pytest.mark.asyncio
    async def test_navigate_timeout_fires(self, relay):
        """When page.goto hangs, asyncio timeout should trigger."""
        page = AsyncMock()

        # Make goto hang forever
        async def hang_forever(*args, **kwargs):
            await asyncio.sleep(999)

        page.goto = hang_forever
        page.title = AsyncMock(return_value="")
        page.url = "about:blank"

        # page.locator() is sync, returning a mock
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(return_value=None)
        page.locator = MagicMock(return_value=locator)

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.navigate("s1", "https://example.com")

        # Should return partial result due to timeout
        assert result["partial"] is True
        assert result["content"] == []

    @pytest.mark.asyncio
    async def test_navigate_partial_flag_on_timeout(self, relay):
        """Verify response includes partial: true when content extraction times out."""
        page = AsyncMock()

        # goto works but takes long
        async def slow_goto(*args, **kwargs):
            await asyncio.sleep(0.01)

        page.goto = slow_goto
        page.wait_for_load_state = AsyncMock()
        page.title = AsyncMock(return_value="Slow Page")
        page.url = "https://slow.example.com"
        page.inner_text = AsyncMock(return_value="Some fallback text")

        locator = MagicMock()

        # Make aria_snapshot hang
        async def hang_snapshot():
            await asyncio.sleep(999)

        locator.aria_snapshot = hang_snapshot
        page.locator = MagicMock(return_value=locator)

        session = BrowserSession(session_id="s1", mode="managed", page=page)
        relay._sessions["s1"] = session

        result = await relay.navigate("s1", "https://slow.example.com")

        assert result["partial"] is True
        # Should have fallen back to inner_text
        assert len(result["content"]) > 0
        assert result["content"][0]["role"] == "text"
        assert "Some fallback text" in result["content"][0]["value"]


class TestAriaSnapshotTimeoutFallback:
    """Test _get_accessibility_tree timeout and fallback."""

    @pytest.mark.asyncio
    async def test_aria_snapshot_timeout_fallback(self, relay):
        """When aria_snapshot hangs, fall back to inner_text and set partial flag."""
        page = MagicMock()
        locator = MagicMock()

        async def hang_snapshot():
            await asyncio.sleep(999)

        locator.aria_snapshot = hang_snapshot
        page.locator = MagicMock(return_value=locator)
        page.inner_text = AsyncMock(return_value="Fallback body text content")

        nodes, partial = await relay._get_accessibility_tree(page)

        assert partial is True
        assert len(nodes) == 1
        assert nodes[0]["role"] == "text"
        assert "Fallback body text content" in nodes[0]["value"]

    @pytest.mark.asyncio
    async def test_aria_snapshot_exception_returns_empty(self, relay):
        """Non-timeout exceptions return empty list and partial=False."""
        page = MagicMock()
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(side_effect=RuntimeError("element detached"))
        page.locator = MagicMock(return_value=locator)
        page.inner_text = AsyncMock(return_value="Fallback text")

        nodes, partial = await relay._get_accessibility_tree(page)

        # Falls back to inner_text
        assert len(nodes) == 1
        assert nodes[0]["role"] == "text"
        assert partial is True


class TestDeadSessionDetection:
    """Test dead session detection and auto-removal."""

    def test_dead_managed_session_detected(self, relay):
        """When container is stopped, SessionDead should be raised."""
        session = BrowserSession(
            session_id="dead-1",
            mode="managed",
            container_id="dead-container-id",
        )
        relay._sessions["dead-1"] = session

        with patch.object(BrowserRelay, "_is_container_running", return_value=False):
            with pytest.raises(SessionDead, match="dead"):
                relay._get_session("dead-1")

    def test_dead_session_auto_removed(self, relay):
        """Dead session should be removed from _sessions dict."""
        session = BrowserSession(
            session_id="dead-2",
            mode="managed",
            container_id="dead-container-id-2",
        )
        relay._sessions["dead-2"] = session

        with patch.object(BrowserRelay, "_is_container_running", return_value=False):
            with pytest.raises(SessionDead):
                relay._get_session("dead-2")

        assert "dead-2" not in relay._sessions

    def test_alive_managed_session_ok(self, relay):
        """A running container should not raise SessionDead."""
        session = BrowserSession(
            session_id="alive-1",
            mode="managed",
            container_id="alive-container",
        )
        relay._sessions["alive-1"] = session

        with patch.object(BrowserRelay, "_is_container_running", return_value=True):
            result = relay._get_session("alive-1")
            assert result is session

    def test_relay_session_not_container_checked(self, relay):
        """Relay sessions should not trigger container health checks."""
        session = BrowserSession(
            session_id="relay-1",
            mode="relay",
        )
        relay._sessions["relay-1"] = session

        with patch.object(BrowserRelay, "_is_container_running") as mock_check:
            result = relay._get_session("relay-1")
            assert result is session
            mock_check.assert_not_called()

    def test_dead_native_session_detected(self, relay):
        """When Chrome process has exited, SessionDead should be raised."""
        process_mock = MagicMock()
        process_mock.poll.return_value = 1  # process exited

        session = BrowserSession(
            session_id="native-dead",
            mode="native",
            process=process_mock,
        )
        relay._sessions["native-dead"] = session

        with pytest.raises(SessionDead, match="dead"):
            relay._get_session("native-dead")

        assert "native-dead" not in relay._sessions

    def test_alive_native_session_ok(self, relay):
        """A running Chrome process should not raise SessionDead."""
        process_mock = MagicMock()
        process_mock.poll.return_value = None  # still running

        session = BrowserSession(
            session_id="native-alive",
            mode="native",
            process=process_mock,
        )
        relay._sessions["native-alive"] = session

        result = relay._get_session("native-alive")
        assert result is session


class TestResourceBlocking:
    """Test resource blocking in page handlers."""

    @pytest.mark.asyncio
    async def test_resource_blocking_installed(self, relay):
        """Verify page.route is called when block_heavy_resources is True."""
        page = MagicMock()
        page.route = AsyncMock()
        await relay._install_page_handlers(page)

        # Check dialog, popup, download handlers are registered
        on_calls = [c[0][0] for c in page.on.call_args_list]
        assert "dialog" in on_calls
        assert "popup" in on_calls
        assert "download" in on_calls

        # Check resource blocking route is registered
        page.route.assert_called_once()
        args = page.route.call_args
        assert args[0][0] == "**/*"

    @pytest.mark.asyncio
    async def test_resource_blocking_not_installed_when_disabled(self, relay_no_blocking):
        """Verify page.route is NOT called when block_heavy_resources is False."""
        page = MagicMock()
        page.route = AsyncMock()
        await relay_no_blocking._install_page_handlers(page)

        page.route.assert_not_called()

    @pytest.mark.asyncio
    async def test_resource_blocking_aborts_images(self, relay):
        """Verify the route handler aborts image/media/font requests."""
        page = MagicMock()
        page.route = AsyncMock()
        await relay._install_page_handlers(page)

        # Get the route handler function
        route_handler = page.route.call_args[0][1]

        for resource_type in BLOCKED_RESOURCE_TYPES:
            route = AsyncMock()
            route.request.resource_type = resource_type
            await route_handler(route)
            route.abort.assert_called_once()
            route.continue_.assert_not_called()

    @pytest.mark.asyncio
    async def test_resource_blocking_allows_documents(self, relay):
        """Verify HTML/XHR/fetch requests pass through."""
        page = MagicMock()
        page.route = AsyncMock()
        await relay._install_page_handlers(page)

        route_handler = page.route.call_args[0][1]

        for resource_type in ("document", "xhr", "fetch", "script"):
            route = AsyncMock()
            route.request.resource_type = resource_type
            await route_handler(route)
            route.continue_.assert_called_once()
            route.abort.assert_not_called()


class TestCleanupLoop:
    """Test session cleanup loop."""

    @pytest.mark.asyncio
    async def test_cleanup_loop_removes_expired_sessions(self):
        """Expired sessions should be cleaned up."""
        relay = BrowserRelay(session_timeout_minutes=1)

        browser_mock = AsyncMock()
        expired_session = BrowserSession(
            session_id="expired",
            mode="relay",
            browser=browser_mock,
            last_used=time.time() - 120,  # 2 minutes ago, timeout is 1 min
        )
        relay._sessions["expired"] = expired_session

        # Run one cleanup cycle manually
        now = time.time()
        expired = [
            sid for sid, s in relay._sessions.items() if now - s.last_used > relay._session_timeout
        ]
        for sid in expired:
            await relay.close_session(sid)

        assert "expired" not in relay._sessions

    @pytest.mark.asyncio
    async def test_cleanup_loop_preserves_active_sessions(self):
        """Recent sessions should survive cleanup."""
        relay = BrowserRelay(session_timeout_minutes=10)

        page_mock = MagicMock()
        page_mock.url = "https://example.com"
        active_session = BrowserSession(
            session_id="active",
            mode="relay",
            page=page_mock,
            last_used=time.time(),  # just used
        )
        relay._sessions["active"] = active_session

        # Run one cleanup cycle manually
        now = time.time()
        expired = [
            sid for sid, s in relay._sessions.items() if now - s.last_used > relay._session_timeout
        ]
        for sid in expired:
            await relay.close_session(sid)

        assert "active" in relay._sessions


class TestContainerConfig:
    """Test container configuration is passed through."""

    @patch("bridge.browser.subprocess.run")
    def test_container_memory_configurable(self, mock_run):
        """Verify _start_chromium_container uses passed memory value."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
            MagicMock(returncode=0, stdout="127.0.0.1:55432\n", stderr=""),
        ]

        _start_chromium_container(memory="2048m")

        run_cmd = mock_run.call_args_list[0][0][0]
        assert "--memory=2048m" in run_cmd

    @patch("bridge.browser.subprocess.run")
    def test_container_shm_size_in_docker_cmd(self, mock_run):
        """Verify --shm-size appears in Docker command."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
            MagicMock(returncode=0, stdout="127.0.0.1:55432\n", stderr=""),
        ]

        _start_chromium_container(shm_size="512m")

        run_cmd = mock_run.call_args_list[0][0][0]
        assert "--shm-size=512m" in run_cmd

    @patch("bridge.browser.subprocess.run")
    def test_container_tmpfs_size_configurable(self, mock_run):
        """Verify tmpfs size is configurable."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
            MagicMock(returncode=0, stdout="127.0.0.1:55432\n", stderr=""),
        ]

        _start_chromium_container(tmpfs_size="256M")

        run_cmd = mock_run.call_args_list[0][0][0]
        # Find the tmpfs arg
        tmpfs_idx = run_cmd.index("--tmpfs")
        tmpfs_val = run_cmd[tmpfs_idx + 1]
        assert "256M" in tmpfs_val
