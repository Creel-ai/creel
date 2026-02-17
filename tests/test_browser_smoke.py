"""Smoke tests for the browser tool against a live bridge + Docker.

These tests require:
  - Docker running (for managed mode Chromium containers)
  - Playwright installed with chromium (`python -m playwright install chromium`)
  - Network access to https://example.com

Run with:
    .venv/bin/python -m pytest tests/test_browser_smoke.py -v -m smoke --no-cov
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from bridge.browser import BrowserRelay

# Mark the entire module as smoke tests
pytestmark = [pytest.mark.smoke, pytest.mark.asyncio]


def _docker_available() -> bool:
    """Check if Docker is running."""
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _playwright_available() -> bool:
    """Check if Playwright + Chromium are installed."""
    try:
        from playwright.async_api import async_playwright  # noqa: F401

        return True
    except ImportError:
        return False


skip_reason = []
if not _docker_available():
    skip_reason.append("Docker not running")
if not _playwright_available():
    skip_reason.append("Playwright not installed")

skip_if_missing = pytest.mark.skipif(
    bool(skip_reason), reason=", ".join(skip_reason) if skip_reason else ""
)


@pytest_asyncio.fixture
async def relay():
    """Create and start a real BrowserRelay, shut it down after the test."""
    r = BrowserRelay(
        max_sessions=3,
        session_timeout_minutes=2,
        blocked_domains=["evil.com"],
        max_content_chars=10000,
    )
    await r.start()
    yield r
    await r.stop()


@skip_if_missing
class TestManagedModeSmoke:
    """End-to-end tests using managed (Docker) browser sessions."""

    async def test_full_lifecycle(self, relay: BrowserRelay):
        """Open session, navigate, get content, get links, close."""
        # 1. Open a managed session
        session_id = await relay.create_managed()
        assert session_id
        assert session_id in [s["session_id"] for s in relay.list_sessions()]

        # 2. Navigate to example.com
        result = await relay.navigate(session_id, "https://example.com")
        assert result["title"] == "Example Domain"
        assert result["url"] == "https://example.com/"
        assert len(result["content"]) > 0

        # Verify we got a heading node
        heading = next(
            (n for n in result["content"] if n["role"] == "heading"), None
        )
        assert heading is not None
        assert "Example Domain" in heading.get("name", "")

        # 3. Get content separately
        content_result = await relay.get_content(session_id)
        assert content_result["title"] == "Example Domain"
        assert len(content_result["content"]) > 0

        # 4. Get links
        links = await relay.get_links(session_id)
        assert isinstance(links, list)
        # example.com has a "More information..." link
        assert len(links) >= 1
        assert any("iana.org" in link.get("href", "") for link in links)

        # 5. Close session
        await relay.close_session(session_id)
        assert session_id not in [s["session_id"] for s in relay.list_sessions()]

    async def test_screenshot(self, relay: BrowserRelay):
        """Open session, navigate, take screenshot."""
        session_id = await relay.create_managed()
        await relay.navigate(session_id, "https://example.com")

        result = await relay.screenshot(session_id, full_page=False)
        assert "base64_image" in result
        assert len(result["base64_image"]) > 100  # non-trivial image data

        await relay.close_session(session_id)

    async def test_blocked_url_rejected(self, relay: BrowserRelay):
        """Verify javascript: URL is blocked at the relay level."""
        session_id = await relay.create_managed()

        with pytest.raises(ValueError, match="not allowed"):
            await relay.navigate(session_id, "javascript:alert(1)")

        with pytest.raises(ValueError, match="not allowed"):
            await relay.navigate(session_id, "file:///etc/passwd")

        with pytest.raises(ValueError, match="blocked"):
            await relay.navigate(session_id, "https://evil.com/steal")

        await relay.close_session(session_id)

    async def test_navigate_multiple_pages(self, relay: BrowserRelay):
        """Navigate to multiple pages in sequence within one session."""
        session_id = await relay.create_managed()

        # First page
        r1 = await relay.navigate(session_id, "https://example.com")
        assert r1["title"] == "Example Domain"

        # Second page
        r2 = await relay.navigate(session_id, "https://www.iana.org/help/example-domains")
        assert r2["url"].startswith("https://www.iana.org")
        assert len(r2["content"]) > 0

        await relay.close_session(session_id)

    async def test_session_cleaned_up_on_close(self, relay: BrowserRelay):
        """Verify Docker container is cleaned up when session closes."""
        import subprocess

        session_id = await relay.create_managed()

        # Get container ID from session internals
        session = relay._sessions[session_id]
        container_id = session.container_id
        assert container_id

        # Container should be running
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.stdout.strip() == "true"

        # Close and verify container is gone
        await relay.close_session(session_id)
        result = subprocess.run(
            ["docker", "inspect", container_id],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Container should no longer exist (--rm flag)
        assert result.returncode != 0
