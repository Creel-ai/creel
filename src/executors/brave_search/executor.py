#!/usr/bin/env python3
"""Brave Search executor - web search via the Brave Search API.

Requires BRAVE_API_KEY environment variable.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys

import requests

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"

# Default HTTP settings
DEFAULT_TIMEOUT = 15.0
DEFAULT_CONNECT_TIMEOUT = 5.0


def search(
    query: str,
    count: int = 5,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> list[dict]:
    """Search the web using Brave Search API.

    Args:
        query: Search query string.
        count: Number of results (1-20).
        timeout: Total request timeout in seconds (hard limit: 120s).
        connect_timeout: Connection timeout in seconds (hard limit: 120s).
    """
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        raise RuntimeError("BRAVE_API_KEY is not set")

    count = max(1, min(count, 20))

    try:
        resp = requests.get(
            BRAVE_API_URL,
            headers={"X-Subscription-Token": api_key},
            params={"q": query, "count": count},  # type: ignore[arg-type]
            timeout=(connect_timeout, timeout),
        )
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Connection failed: could not connect to Brave Search API") from None
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Brave Search request timed out after {timeout}s (connect timeout: {connect_timeout}s)"
        ) from None
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("web", {}).get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            }
        )

    return results


def main() -> None:
    query = os.environ.get("QUERY", "")
    if not query and len(sys.argv) > 1:
        query = sys.argv[1]

    if not query:
        print(json.dumps({"error": "QUERY is required"}), file=sys.stderr)
        sys.exit(1)

    count = int(os.environ.get("COUNT", "5"))

    try:
        results = search(query, count)
        print(json.dumps(results, indent=2))
    except requests.HTTPError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
