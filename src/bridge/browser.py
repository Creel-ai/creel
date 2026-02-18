"""Browser relay service for Creel bridge.

Manages browser sessions via Playwright CDP connections. Supports three modes:
- relay: connects to user's Chrome (launched with --remote-debugging-port)
- managed: spawns a headless Chromium Docker container and connects via CDP
- native: launches a local Chrome/Chromium subprocess with a fresh temp profile

Primary output is the accessibility tree (structured, token-efficient).
Screenshot fallback available when visual context is needed.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import platform
import shutil
import subprocess
import tempfile
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

# Resource types to block for memory savings (images, media, fonts, stylesheets)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


class BrowserError(Exception):
    """Base exception for browser relay errors."""


class SessionDead(BrowserError):
    """Raised when a browser session is no longer responsive."""


@dataclass
class BrowserSession:
    """Tracks a single browser session."""

    session_id: str
    mode: str  # "relay" | "managed" | "native"
    browser: Any = None  # playwright Browser
    page: Any = None  # playwright Page
    container_id: str | None = None  # Docker container ID (managed mode)
    process: Any = None  # subprocess.Popen (native mode)
    temp_profile_dir: str | None = None  # temp user-data-dir (native mode)
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
        container_memory: str = "1024m",
        container_shm_size: str = "256m",
        container_tmpfs_size: str = "128M",
        navigate_timeout_ms: int = 30000,
        snapshot_timeout_ms: int = 15000,
        block_heavy_resources: bool = True,
    ):
        self._playwright: Any = None
        self._sessions: dict[str, BrowserSession] = {}
        self._max_sessions = max_sessions
        self._session_timeout = session_timeout_minutes * 60  # convert to seconds
        self._blocked_domains = set(blocked_domains or [])
        self._max_content_chars = max_content_chars
        self._container_memory = container_memory
        self._container_shm_size = container_shm_size
        self._container_tmpfs_size = container_tmpfs_size
        self._navigate_timeout_ms = navigate_timeout_ms
        self._snapshot_timeout_ms = snapshot_timeout_ms
        self._block_heavy_resources = block_heavy_resources
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Initialize Playwright and start the session cleanup task."""
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

    async def create_native(self, headless: bool = True) -> str:
        """Launch a local Chrome/Chromium subprocess and connect via CDP.

        Uses a fresh temporary profile directory for credential isolation.

        Returns:
            session_id for subsequent commands
        """
        self._check_session_limit()

        chrome_binary = _find_chrome_binary()
        process, port, temp_dir = _start_native_chrome(chrome_binary, headless)

        try:
            cdp_url = f"http://localhost:{port}"
            await self._wait_for_cdp(cdp_url)

            chromium = self._playwright.chromium
            browser = await chromium.connect_over_cdp(cdp_url)

            contexts = browser.contexts
            if contexts and contexts[0].pages:
                page = contexts[0].pages[0]
            else:
                context = await browser.new_context()
                page = await context.new_page()

            await self._install_page_handlers(page)
        except Exception:
            process.terminate()
            process.wait(timeout=5)
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        session_id = str(uuid.uuid4())
        self._sessions[session_id] = BrowserSession(
            session_id=session_id,
            mode="native",
            browser=browser,
            page=page,
            process=process,
            temp_profile_dir=temp_dir,
        )

        logger.info(
            "Native session %s started (pid=%d, port=%d)",
            session_id,
            process.pid,
            port,
        )
        return session_id

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

        await self._install_page_handlers(page)

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
        container_id, host_port = _start_chromium_container(
            headless,
            memory=self._container_memory,
            shm_size=self._container_shm_size,
            tmpfs_size=self._container_tmpfs_size,
        )

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

            await self._install_page_handlers(page)
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
            Dict with title, url, and content (accessibility tree nodes).
            May include ``partial: True`` if content extraction timed out.
        """
        self._validate_url(url)
        session = self._get_session(session_id)

        partial = False

        async with session.lock:
            session.last_used = time.time()
            try:
                # Outer ceiling: navigate_timeout + 15s for content extraction
                async def _do_navigate():
                    nonlocal partial
                    await session.page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self._navigate_timeout_ms,
                    )
                    try:
                        await session.page.wait_for_load_state(
                            "networkidle", timeout=10000
                        )
                    except Exception:
                        pass  # Continue with whatever has loaded; SPAs may never idle

                    title = await session.page.title()
                    current_url = session.page.url
                    content, was_partial = await self._get_accessibility_tree(
                        session.page
                    )
                    partial = was_partial
                    return title, current_url, content

                title, current_url, content = await asyncio.wait_for(
                    _do_navigate(),
                    timeout=(self._navigate_timeout_ms / 1000) + 15,
                )
            except asyncio.TimeoutError:
                # Navigation itself timed out — return what we can
                title = ""
                try:
                    title = await asyncio.wait_for(
                        session.page.title(), timeout=2
                    )
                except Exception:
                    pass
                current_url = session.page.url
                content = []
                partial = True

        result: dict[str, Any] = {
            "title": title,
            "url": current_url,
            "content": content,
        }
        if partial:
            result["partial"] = True
        return result

    async def get_content(
        self, session_id: str, selector: str | None = None
    ) -> dict[str, Any]:
        """Get the current page content as an accessibility tree.

        Args:
            session_id: Active session ID
            selector: Optional CSS selector to scope content extraction

        Returns:
            Dict with title, url, and content nodes.
            May include ``partial: True`` if content extraction timed out.
        """
        session = self._get_session(session_id)

        async with session.lock:
            session.last_used = time.time()
            title = await session.page.title()
            current_url = session.page.url
            content, partial = await self._get_accessibility_tree(
                session.page, selector
            )

        result: dict[str, Any] = {
            "title": title,
            "url": current_url,
            "content": content,
        }
        if partial:
            result["partial"] = True
        return result

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
            await session.page.click(selector, timeout=self._navigate_timeout_ms)
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
            await session.page.fill(selector, text, timeout=self._navigate_timeout_ms)
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
        For native sessions, terminates the Chrome process and cleans up temp dir.
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
        elif session.mode == "native":
            if session.process:
                try:
                    session.process.terminate()
                    session.process.wait(timeout=5)
                except Exception as e:
                    logger.warning("Error terminating native Chrome for session %s: %s", session_id, e)
            if session.temp_profile_dir:
                shutil.rmtree(session.temp_profile_dir, ignore_errors=True)

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

    async def _install_page_handlers(self, page: Any) -> None:
        """Register handlers for dialogs, popups, downloads, and resource blocking.

        Prevents malicious pages from hanging the session with alert()
        or spawning untracked popups. Optionally blocks heavy resources
        (images, media, fonts, stylesheets) to save memory.
        """
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("popup", lambda popup: popup.close())
        page.on("download", lambda download: download.cancel())

        if self._block_heavy_resources:

            async def _block_resources(route):
                if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _block_resources)

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

        # Check if session is still alive for managed mode
        if session.mode == "managed" and session.container_id:
            if not self._is_container_running(session.container_id):
                self._sessions.pop(session_id, None)
                raise SessionDead(
                    f"Session {session_id} is dead (container stopped). "
                    "Open a new session with browser_open."
                )

        # Check if session is still alive for native mode
        if session.mode == "native" and session.process:
            if session.process.poll() is not None:
                self._sessions.pop(session_id, None)
                raise SessionDead(
                    f"Session {session_id} is dead (Chrome process exited). "
                    "Open a new session with browser_open."
                )

        return session

    @staticmethod
    def _is_container_running(container_id: str) -> bool:
        """Check if a Docker container is still running."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"
        except Exception:
            return False

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
    ) -> tuple[list[dict[str, Any]], bool]:
        """Extract the accessibility tree from the page.

        Args:
            page: Playwright page object.
            selector: Optional CSS selector to scope to a subtree.

        Returns a tuple of (nodes, partial) where nodes is a list of node
        dicts with role, name, value, level, and partial is True if the
        content extraction fell back to inner_text due to a timeout.
        Truncated at max_content_chars.

        Uses Playwright's locator.aria_snapshot() which returns a YAML-like
        text representation of the accessibility tree.
        """
        import re

        locator = page.locator(selector) if selector else page.locator("body")

        snapshot = None
        partial = False
        try:
            snapshot = await asyncio.wait_for(
                locator.aria_snapshot(),
                timeout=self._snapshot_timeout_ms / 1000,
            )
        except Exception as exc:
            # Fallback: try to get plain text content instead
            is_timeout = isinstance(exc, asyncio.TimeoutError)
            if is_timeout:
                logger.warning("aria_snapshot timed out, falling back to inner_text")
            try:
                body_text = await asyncio.wait_for(
                    page.inner_text("body"), timeout=5
                )
                if body_text:
                    truncated = body_text[: self._max_content_chars]
                    return [{"role": "text", "value": truncated}], True
            except Exception:
                pass
            return [], is_timeout

        if not snapshot or not snapshot.strip():
            return [], False

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

        return nodes, partial

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
        """Periodically clean up idle sessions."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            now = time.time()
            expired = [
                sid
                for sid, s in self._sessions.items()
                if now - s.last_used > self._session_timeout
            ]
            for sid in expired:
                logger.info("Cleaning up idle session %s", sid)
                try:
                    await self.close_session(sid)
                except Exception as e:
                    logger.warning("Error cleaning up session %s: %s", sid, e)


def _start_chromium_container(
    headless: bool = True,
    memory: str = "1024m",
    shm_size: str = "256m",
    tmpfs_size: str = "128M",
) -> tuple[str, int]:
    """Start a headless Chromium Docker container.

    Lets Docker assign a random host port to avoid TOCTOU race conditions.

    Args:
        headless: Launch in headless mode.
        memory: Container memory limit (e.g., "1024m").
        shm_size: Shared memory size (Chrome uses /dev/shm heavily).
        tmpfs_size: tmpfs mount size for /tmp.

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
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,size={tmpfs_size}",
        f"--memory={memory}",
        f"--shm-size={shm_size}",
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


def _find_chrome_binary() -> str:
    """Find a locally-installed Chrome/Chromium binary.

    Returns:
        Path to the Chrome executable.

    Raises:
        FileNotFoundError: If no Chrome installation is found.
    """
    system = platform.system()

    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
    elif system == "Linux":
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            path = shutil.which(name)
            if path:
                return path

    raise FileNotFoundError(
        f"No Chrome/Chromium installation found on {system}. "
        "Install Google Chrome or Chromium, or use 'managed' mode instead."
    )


def _start_native_chrome(
    chrome_binary: str, headless: bool = True
) -> tuple[Any, int, str]:
    """Launch Chrome as a subprocess with a fresh temp profile.

    Args:
        chrome_binary: Path to Chrome executable.
        headless: Launch in headless mode.

    Returns:
        Tuple of (process, cdp_port, temp_profile_dir).
    """
    import re as _re

    temp_dir = tempfile.mkdtemp(prefix="creel-chrome-")

    cmd = [
        chrome_binary,
        "--remote-debugging-port=0",  # OS assigns a free port
        f"--user-data-dir={temp_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
    ]

    if headless:
        cmd.append("--headless=new")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Chrome prints "DevTools listening on ws://127.0.0.1:PORT/..." to stderr
    port = None
    deadline = time.time() + 15
    while time.time() < deadline:
        line = process.stderr.readline()
        if not line:
            if process.poll() is not None:
                break
            time.sleep(0.1)
            continue
        decoded = line.decode("utf-8", errors="replace")
        m = _re.search(r"DevTools listening on ws://[\w.]+:(\d+)/", decoded)
        if m:
            port = int(m.group(1))
            break

    if port is None:
        process.terminate()
        process.wait(timeout=5)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("Failed to detect Chrome CDP port from stderr output")

    logger.info("Started native Chrome (pid=%d, port=%d)", process.pid, port)
    return process, port, temp_dir
