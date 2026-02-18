"""Browser relay service for Creel bridge.

Manages browser sessions via Playwright CDP connections. Supports two modes:
- relay: connects to user's Chrome (launched with --remote-debugging-port)
- managed: spawns a headless Chromium Docker container and connects via CDP

Primary output is the accessibility tree (structured, token-efficient).
Screenshot fallback available when visual context is needed.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Only these URL schemes are permitted
ALLOWED_SCHEMES = {"http", "https"}

# Default Chromium Docker image
DEFAULT_CHROMIUM_IMAGE = "zenika/alpine-chrome:latest"

# Label applied to managed Chromium containers for orphan detection
CREEL_CONTAINER_LABEL = "creel.browser=managed"


@dataclass
class BrowserSession:
    """Tracks a single browser session."""

    session_id: str
    mode: str  # "relay" | "managed"
    browser: Any = None  # playwright Browser
    page: Any = None  # playwright Page
    container_id: str | None = None  # Docker container ID (managed mode)
    last_used: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserRelay:
    """Manages browser sessions via Playwright CDP connections.

    Both relay and managed modes use CDP — the difference is what's
    on the other end of the connection.
    """

    def __init__(
        self,
        max_sessions: int = 3,
        session_timeout_minutes: int = 10,
        blocked_domains: list[str] | None = None,
        max_content_chars: int = 10000,
    ):
        self._playwright: Any = None
        self._sessions: dict[str, BrowserSession] = {}
        self._max_sessions = max_sessions
        self._session_timeout = session_timeout_minutes * 60  # convert to seconds
        self._blocked_domains = set(blocked_domains or [])
        self._max_content_chars = max_content_chars
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Initialize Playwright and start the session cleanup task."""
        reaped = _reap_orphaned_containers()
        if reaped:
            logger.info("Cleaned up %d orphaned container(s) from previous run", reaped)

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("BrowserRelay started")

    async def stop(self) -> None:
        """Shut down all sessions and Playwright."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Close all sessions (including managed containers)
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            try:
                await self.close_session(sid)
            except Exception as e:
                logger.warning("Error closing session %s during shutdown: %s", sid, e)

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        logger.info("BrowserRelay stopped")

    async def connect_relay(self, cdp_url: str) -> str:
        """Connect to a user's Chrome instance via CDP.

        Args:
            cdp_url: Chrome DevTools Protocol URL
                     (e.g., http://localhost:9222)

        Returns:
            session_id for subsequent commands

        Raises:
            ValueError: If cdp_url does not point to localhost
        """
        self._check_session_limit()
        self._validate_cdp_url(cdp_url)

        chromium = self._playwright.chromium
        browser = await chromium.connect_over_cdp(cdp_url)

        # Use existing page or create new one
        contexts = browser.contexts
        if contexts and contexts[0].pages:
            page = contexts[0].pages[0]
        else:
            context = await browser.new_context()
            page = await context.new_page()

        self._install_page_handlers(page)

        session_id = str(uuid.uuid4())
        self._sessions[session_id] = BrowserSession(
            session_id=session_id,
            mode="relay",
            browser=browser,
            page=page,
        )

        logger.info("Relay session %s connected to %s", session_id, cdp_url)
        return session_id

    async def create_managed(self, headless: bool = True) -> str:
        """Launch a headless Chromium Docker container and connect via CDP.

        Returns:
            session_id for subsequent commands
        """
        self._check_session_limit()

        # Launch Chromium container; Docker assigns a random host port
        container_id, host_port = _start_chromium_container(headless)

        # Connect to CDP, cleaning up the container on any failure
        try:
            cdp_url = f"http://localhost:{host_port}"
            await self._wait_for_cdp(cdp_url)

            chromium = self._playwright.chromium
            browser = await chromium.connect_over_cdp(cdp_url)

            contexts = browser.contexts
            if contexts and contexts[0].pages:
                page = contexts[0].pages[0]
            else:
                context = await browser.new_context()
                page = await context.new_page()

            self._install_page_handlers(page)
        except Exception:
            _stop_container(container_id)
            raise

        session_id = str(uuid.uuid4())
        self._sessions[session_id] = BrowserSession(
            session_id=session_id,
            mode="managed",
            browser=browser,
            page=page,
            container_id=container_id,
        )

        logger.info(
            "Managed session %s started (container=%s, port=%d)",
            session_id,
            container_id[:12],
            host_port,
        )
        return session_id

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        """Navigate to a URL and return the page content.

        Args:
            session_id: Active session ID
            url: URL to navigate to

        Returns:
            Dict with title, url, and content (accessibility tree nodes)
        """
        self._validate_url(url)
        session = self._get_session(session_id)

        async with session.lock:
            session.last_used = time.time()
            await session.page.goto(url, wait_until="domcontentloaded")
            try:
                await session.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # Continue with whatever has loaded; SPAs may never idle

            title = await session.page.title()
            current_url = session.page.url
            content = await self._get_accessibility_tree(session.page)

        return {
            "title": title,
            "url": current_url,
            "content": content,
        }

    async def get_content(
        self, session_id: str, selector: str | None = None
    ) -> dict[str, Any]:
        """Get the current page content as an accessibility tree.

        Args:
            session_id: Active session ID
            selector: Optional CSS selector to scope content extraction

        Returns:
            Dict with title, url, and content nodes
        """
        session = self._get_session(session_id)

        async with session.lock:
            session.last_used = time.time()
            title = await session.page.title()
            current_url = session.page.url
            content = await self._get_accessibility_tree(session.page, selector)

        return {
            "title": title,
            "url": current_url,
            "content": content,
        }

    async def click(self, session_id: str, selector: str) -> dict[str, Any]:
        """Click an element on the page.

        Args:
            session_id: Active session ID
            selector: CSS selector for the element to click

        Returns:
            Dict with ok status and new URL
        """
        session = self._get_session(session_id)

        async with session.lock:
            session.last_used = time.time()
            await session.page.click(selector)
            # Wait for potential navigation
            try:
                await session.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # Page may not navigate after click

            # Validate the resulting URL in case click triggered navigation
            # to a blocked scheme or domain
            current_url = session.page.url
            try:
                self._validate_url(current_url)
            except ValueError:
                await session.page.go_back()
                raise

            return {
                "ok": True,
                "url": current_url,
            }

    async def type_text(
        self, session_id: str, selector: str, text: str
    ) -> dict[str, Any]:
        """Type text into an input element.

        Args:
            session_id: Active session ID
            selector: CSS selector for the input element
            text: Text to type

        Returns:
            Dict with ok status and current URL
        """
        session = self._get_session(session_id)

        async with session.lock:
            session.last_used = time.time()
            await session.page.fill(selector, text)
            # Wait briefly for any form-triggered navigation
            try:
                await session.page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass

            # Validate resulting URL in case form submission navigated
            current_url = session.page.url
            try:
                self._validate_url(current_url)
            except ValueError:
                await session.page.go_back()
                raise

            return {"ok": True, "url": current_url}

    async def screenshot(
        self, session_id: str, full_page: bool = False
    ) -> dict[str, Any]:
        """Take a screenshot of the current page.

        Args:
            session_id: Active session ID
            full_page: If True, capture the full scrollable page

        Returns:
            Dict with base64-encoded PNG image
        """
        session = self._get_session(session_id)

        async with session.lock:
            session.last_used = time.time()
            raw = await session.page.screenshot(full_page=full_page)
            b64 = base64.b64encode(raw).decode("ascii")

            return {
                "base64_image": b64,
                "url": session.page.url,
            }

    async def get_links(self, session_id: str) -> list[dict[str, str]]:
        """Get all links on the current page.

        Args:
            session_id: Active session ID

        Returns:
            List of dicts with text and href for each link
        """
        session = self._get_session(session_id)

        async with session.lock:
            session.last_used = time.time()
            links = await session.page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    text: a.innerText.trim().substring(0, 200),
                    href: a.href
                })).filter(l => l.text && l.href)
            """)
            return links

    async def close_session(self, session_id: str) -> None:
        """Close a browser session and clean up resources.

        For managed sessions, also stops the Docker container.
        """
        session = self._sessions.pop(session_id, None)
        if not session:
            return

        try:
            if session.browser:
                await session.browser.close()
        except Exception as e:
            logger.warning("Error closing browser for session %s: %s", session_id, e)

        if session.mode == "managed" and session.container_id:
            _stop_container(session.container_id)

        logger.info("Session %s closed (mode=%s)", session_id, session.mode)

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions."""
        return [
            {
                "session_id": s.session_id,
                "mode": s.mode,
                "last_used": s.last_used,
                "url": s.page.url if s.page else None,
            }
            for s in self._sessions.values()
        ]

    # --- Internal helpers ---

    @staticmethod
    def _validate_cdp_url(cdp_url: str) -> None:
        """Ensure CDP URL points to localhost only (prevent SSRF)."""
        parsed = urlparse(cdp_url)
        hostname = parsed.hostname or ""
        allowed = {"localhost", "127.0.0.1", "::1"}
        if hostname not in allowed:
            raise ValueError(
                f"CDP URL must point to localhost, got '{hostname}'"
            )

    @staticmethod
    def _install_page_handlers(page: Any) -> None:
        """Register handlers for dialogs, popups, and downloads.

        Prevents malicious pages from hanging the session with alert()
        or spawning untracked popups.
        """
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("popup", lambda popup: popup.close())
        page.on("download", lambda download: download.cancel())

    def _check_session_limit(self) -> None:
        if len(self._sessions) >= self._max_sessions:
            raise RuntimeError(
                f"Maximum sessions ({self._max_sessions}) reached. "
                "Close an existing session first."
            )

    def _get_session(self, session_id: str) -> BrowserSession:
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Unknown session: {session_id}")
        if session.browser and not session.browser.is_connected():
            asyncio.get_event_loop().create_task(self.close_session(session_id))
            raise ValueError(
                f"Session {session_id} has a dead browser connection"
            )
        return session

    def _validate_url(self, url: str) -> None:
        """Validate URL scheme (allowlist) and blocked domains."""
        parsed = urlparse(url)

        if not parsed.scheme:
            raise ValueError("URL must include a scheme (e.g., https://)")

        if parsed.scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"URL scheme '{parsed.scheme}' is not allowed")

        hostname = parsed.hostname or ""
        for blocked in self._blocked_domains:
            if hostname == blocked or hostname.endswith(f".{blocked}"):
                raise ValueError(f"Domain '{hostname}' is blocked")

    async def _get_accessibility_tree(
        self, page: Any, selector: str | None = None
    ) -> list[dict[str, Any]]:
        """Extract the accessibility tree from the page.

        Args:
            page: Playwright page object.
            selector: Optional CSS selector to scope to a subtree.

        Returns a list of node dicts with role, name, value, level.
        Truncated at max_content_chars.

        Uses Playwright's locator.aria_snapshot() which returns a YAML-like
        text representation of the accessibility tree.
        """
        import re

        locator = page.locator(selector) if selector else page.locator("body")
        try:
            snapshot = await locator.aria_snapshot()
        except Exception:
            return []

        if not snapshot or not snapshot.strip():
            return []

        nodes: list[dict[str, Any]] = []
        total_chars = 0

        # Pattern: "- role \"name\" [attr=value]: text_value"
        # Indentation (number of leading spaces) determines depth.
        # Name may contain escaped quotes (e.g., "Say \"Hello\"").
        line_re = re.compile(
            r'^(?P<indent>\s*)-\s+'
            r'(?P<role>\w+)'
            r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
            r'(?:\s+\[(?P<attrs>[^\]]*)\])?'
            r'(?::\s*(?P<value>.+))?$'
        )

        for line in snapshot.splitlines():
            if total_chars >= self._max_content_chars:
                break

            m = line_re.match(line)
            if not m:
                continue

            indent = len(m.group("indent"))
            depth = indent // 2  # aria_snapshot uses 2-space indent
            role = m.group("role")
            name = (m.group("name") or "").replace('\\"', '"')
            value = m.group("value") or ""

            # Parse level from attributes like [level=1]
            attrs_str = m.group("attrs") or ""
            level_from_attrs = None
            if attrs_str:
                level_match = re.search(r'level=(\d+)', attrs_str)
                if level_match:
                    level_from_attrs = int(level_match.group(1))

            entry: dict[str, Any] = {"role": role}
            if name:
                entry["name"] = name
                total_chars += len(name)
            if value:
                entry["value"] = value
                total_chars += len(value)
            if level_from_attrs is not None:
                entry["level"] = level_from_attrs
            elif depth > 0:
                entry["level"] = depth

            # Only include nodes that have meaningful content
            if entry.get("name") or entry.get("value") or role in (
                "heading",
                "link",
                "button",
                "textbox",
                "img",
            ):
                nodes.append(entry)

        return nodes

    async def _wait_for_cdp(self, cdp_url: str, timeout: float = 15.0) -> None:
        """Wait for the CDP endpoint to become available."""
        import httpx

        deadline = time.time() + timeout
        async with httpx.AsyncClient() as client:
            while time.time() < deadline:
                try:
                    resp = await client.get(f"{cdp_url}/json/version", timeout=2.0)
                    if resp.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.5)

        raise TimeoutError(
            f"CDP endpoint {cdp_url} did not become available within {timeout}s"
        )

    async def _cleanup_loop(self) -> None:
        """Periodically clean up idle and dead sessions."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            now = time.time()
            to_close: list[tuple[str, str]] = []  # (session_id, reason)

            for sid, s in self._sessions.items():
                if now - s.last_used > self._session_timeout:
                    to_close.append((sid, "idle"))
                elif s.browser and not s.browser.is_connected():
                    to_close.append((sid, "dead connection"))

            for sid, reason in to_close:
                logger.info("Cleaning up session %s (%s)", sid, reason)
                try:
                    await self.close_session(sid)
                except Exception as e:
                    logger.warning("Error cleaning up session %s: %s", sid, e)


def _start_chromium_container(headless: bool = True) -> tuple[str, int]:
    """Start a headless Chromium Docker container.

    Lets Docker assign a random host port to avoid TOCTOU race conditions.

    Returns:
        Tuple of (container_id, host_port).
    """
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--cap-drop=ALL",
        "--read-only",
        "--label",
        CREEL_CONTAINER_LABEL,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64M",
        "--memory=512m",
        "--cpus=1.0",
        "-p",
        "127.0.0.1::9222",  # random host port, bound to localhost only
        DEFAULT_CHROMIUM_IMAGE,
        "--no-sandbox",
        "--remote-debugging-address=0.0.0.0",
        "--remote-debugging-port=9222",
    ]

    if headless:
        cmd.append("--headless")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to start Chromium container: {result.stderr.strip()}")

    container_id = result.stdout.strip()

    # Query Docker for the actual assigned host port
    port_result = subprocess.run(
        ["docker", "port", container_id, "9222"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if port_result.returncode != 0:
        _stop_container(container_id)
        raise RuntimeError(f"Failed to get container port: {port_result.stderr.strip()}")

    # Output format: "127.0.0.1:XXXXX" or "0.0.0.0:XXXXX"
    port_line = port_result.stdout.strip().splitlines()[0]
    host_port = int(port_line.rsplit(":", 1)[1])

    logger.info("Started Chromium container %s on port %d", container_id[:12], host_port)
    return container_id, host_port


def _reap_orphaned_containers() -> int:
    """Find and stop any orphaned Creel-managed Chromium containers.

    Queries Docker for containers with the CREEL_CONTAINER_LABEL and stops them.
    Called on startup to clean up containers left by a previous crash.

    Returns:
        Number of containers reaped.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"label={CREEL_CONTAINER_LABEL}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("Docker not available for orphan reaping: %s", e)
        return 0

    if result.returncode != 0:
        logger.debug("docker ps failed (rc=%d): %s", result.returncode, result.stderr.strip())
        return 0

    container_ids = result.stdout.strip().splitlines()
    if not container_ids:
        return 0

    reaped = 0
    for cid in container_ids:
        cid = cid.strip()
        if cid:
            logger.info("Reaping orphaned container %s", cid[:12])
            _stop_container(cid)
            reaped += 1

    logger.info("Reaped %d orphaned container(s)", reaped)
    return reaped


def _stop_container(container_id: str) -> None:
    """Stop and remove a Docker container."""
    try:
        subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
        logger.info("Stopped container %s", container_id[:12])
    except Exception as e:
        logger.warning("Error stopping container %s: %s", container_id[:12], e)
