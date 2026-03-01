#!/usr/bin/env python3
"""Browser executor - bridge-calling executor for web browsing.

Instead of running Playwright directly, this executor makes HTTP calls
to the bridge server which manages browser sessions via CDP.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests


def call_bridge(
    endpoint: str,
    data: dict[str, Any] | None = None,
    timeout: int = 30,
    method: str = "POST",
) -> dict:
    """Make an HTTP call to the bridge server.

    Args:
        endpoint: Bridge endpoint path (e.g., '/browser/navigate')
        data: Request body data (optional)
        timeout: Request timeout in seconds
        method: HTTP method (default "POST")

    Returns:
        Bridge response as dict

    Raises:
        RuntimeError: If bridge call fails or returns error
    """
    bridge_url = os.environ.get("BRIDGE_URL")
    bridge_token = os.environ.get("BRIDGE_TOKEN")

    if not bridge_url:
        raise RuntimeError("BRIDGE_URL environment variable not set")
    if not bridge_token:
        raise RuntimeError("BRIDGE_TOKEN environment variable not set")

    url = f"{bridge_url}{endpoint}"
    headers = {
        "Authorization": f"Bearer {bridge_token}",
        "Content-Type": "application/json",
    }

    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=timeout)
        elif data is not None:
            response = requests.post(url, json=data, headers=headers, timeout=timeout)
        else:
            response = requests.post(url, headers=headers, timeout=timeout)

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Bridge error: {result.get('error', 'Unknown error')}")

        return result

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Bridge request failed: {e}") from e


def connect(
    mode: str = "managed",
    cdp_url: str | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    """Create a new browser session via bridge."""
    data: dict[str, Any] = {"mode": mode, "headless": headless}
    if cdp_url:
        data["cdp_url"] = cdp_url
    return call_bridge("/browser/connect", data, timeout=60)


def navigate(session_id: str, url: str) -> dict[str, Any]:
    """Navigate to a URL via bridge."""
    return call_bridge("/browser/navigate", {"session_id": session_id, "url": url}, timeout=60)


def get_content(session_id: str, selector: str | None = None) -> dict[str, Any]:
    """Get current page content via bridge."""
    data: dict[str, Any] = {"session_id": session_id}
    if selector:
        data["selector"] = selector
    return call_bridge("/browser/content", data)


def click(session_id: str, selector: str) -> dict[str, Any]:
    """Click an element via bridge."""
    return call_bridge("/browser/click", {"session_id": session_id, "selector": selector})


def type_text(session_id: str, selector: str, text: str) -> dict[str, Any]:
    """Type text into an input via bridge."""
    return call_bridge(
        "/browser/type", {"session_id": session_id, "selector": selector, "text": text}
    )


def screenshot(session_id: str, full_page: bool = False) -> dict[str, Any]:
    """Take a screenshot via bridge."""
    return call_bridge("/browser/screenshot", {"session_id": session_id, "full_page": full_page})


def get_links(session_id: str) -> dict[str, Any]:
    """Get all links on the page via bridge."""
    return call_bridge("/browser/links", {"session_id": session_id})


def close_session(session_id: str) -> dict[str, Any]:
    """Close a browser session via bridge."""
    return call_bridge("/browser/close", {"session_id": session_id})


def sessions() -> dict[str, Any]:
    """List active browser sessions via bridge."""
    return call_bridge("/browser/sessions", method="GET", timeout=10)


def main() -> None:
    """Main executor entry point."""
    action = os.environ.get("ACTION", "connect")

    try:
        if action == "connect":
            mode = os.environ.get("MODE", "managed")
            cdp_url = os.environ.get("CDP_URL")
            headless = os.environ.get("HEADLESS", "true").lower() in ("true", "1", "yes")
            result = connect(mode, cdp_url, headless)

        elif action == "navigate":
            session_id = os.environ.get("SESSION_ID")
            url = os.environ.get("URL")
            if not session_id:
                raise ValueError("SESSION_ID environment variable required")
            if not url:
                raise ValueError("URL environment variable required")
            result = navigate(session_id, url)

        elif action == "content":
            session_id = os.environ.get("SESSION_ID")
            if not session_id:
                raise ValueError("SESSION_ID environment variable required")
            selector = os.environ.get("SELECTOR")
            result = get_content(session_id, selector)

        elif action == "click":
            session_id = os.environ.get("SESSION_ID")
            selector = os.environ.get("SELECTOR")
            if not session_id:
                raise ValueError("SESSION_ID environment variable required")
            if not selector:
                raise ValueError("SELECTOR environment variable required")
            result = click(session_id, selector)

        elif action == "type":
            session_id = os.environ.get("SESSION_ID")
            selector = os.environ.get("SELECTOR")
            text = os.environ.get("TEXT")
            if not session_id:
                raise ValueError("SESSION_ID environment variable required")
            if not selector:
                raise ValueError("SELECTOR environment variable required")
            if not text:
                raise ValueError("TEXT environment variable required")
            result = type_text(session_id, selector, text)

        elif action == "screenshot":
            session_id = os.environ.get("SESSION_ID")
            if not session_id:
                raise ValueError("SESSION_ID environment variable required")
            full_page = os.environ.get("FULL_PAGE", "false").lower() in ("true", "1", "yes")
            result = screenshot(session_id, full_page)

        elif action == "links":
            session_id = os.environ.get("SESSION_ID")
            if not session_id:
                raise ValueError("SESSION_ID environment variable required")
            result = get_links(session_id)

        elif action == "close":
            session_id = os.environ.get("SESSION_ID")
            if not session_id:
                raise ValueError("SESSION_ID environment variable required")
            result = close_session(session_id)

        elif action == "sessions":
            result = sessions()

        else:
            raise ValueError(f"Unknown action: {action}")

        # Output result as JSON
        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
