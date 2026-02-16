#!/usr/bin/env python3
"""URL content fetcher executor - extracts text from web pages.

No authentication required. Uses requests + BeautifulSoup.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Creel/1.0 (URL Fetcher)"
DEFAULT_MAX_CHARS = 10000


def fetch_url(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """Fetch a URL and extract its text content."""
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
        allow_redirects=True,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")

    if "text/html" in content_type or "application/xhtml" in content_type:
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove script and style elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        text = soup.get_text(separator="\n", strip=True)
    elif "text/" in content_type or "application/json" in content_type:
        title = ""
        text = resp.text
    else:
        return {
            "url": url,
            "title": "",
            "content": f"[Unsupported content type: {content_type}]",
            "content_type": content_type,
        }

    # Collapse multiple blank lines
    lines = text.split("\n")
    cleaned = []
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not prev_blank:
                cleaned.append("")
            prev_blank = True
        else:
            cleaned.append(stripped)
            prev_blank = False
    text = "\n".join(cleaned)

    # Truncate to max_chars
    truncated = len(text) > max_chars
    text = text[:max_chars]

    return {
        "url": url,
        "title": title,
        "content": text,
        "content_type": content_type,
        "truncated": truncated,
    }


def main() -> None:
    url = os.environ.get("URL", "")
    if not url and len(sys.argv) > 1:
        url = sys.argv[1]

    if not url:
        print(json.dumps({"error": "URL is required"}), file=sys.stderr)
        sys.exit(1)

    max_chars = int(os.environ.get("MAX_CHARS", str(DEFAULT_MAX_CHARS)))

    try:
        result = fetch_url(url, max_chars)
        print(json.dumps(result, indent=2))
    except requests.HTTPError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
