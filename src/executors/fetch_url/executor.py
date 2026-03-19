#!/usr/bin/env python3
"""URL content fetcher executor - extracts text from web pages.

No authentication required. Uses requests + BeautifulSoup.
Outputs JSON to stdout.

Includes SSRF protection: blocks private/internal IPs, cloud metadata
endpoints, and non-HTTP schemes. Validates both the URL and the resolved
IP address to prevent DNS rebinding attacks.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Creel/1.0 (URL Fetcher)"


def register_skill():
    """Register the fetch_url skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="fetch_url",
        label="Fetch URL",
        tools=(
            ToolSpec(
                name="fetch_url",
                description="Fetch and extract text content from a URL",
                params=(
                    Param(
                        name="url",
                        type="string",
                        description="URL to fetch",
                        required=True,
                    ),
                    Param(
                        name="max_chars",
                        type="string",
                        description="Max characters to return (default: 10000)",
                    ),
                ),
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        url = config.args.get("url", "")
        max_chars = int(config.args.get("max_chars", "10000"))
        result = fetch_url(
            url,
            max_chars,
            timeout=config.http.timeout,
            connect_timeout=config.http.connect_timeout,
            max_redirects=config.http.max_redirects,
            max_size_mb=config.http.max_size_mb,
        )
        return json.dumps(result, indent=2)

    return meta, execute


DEFAULT_MAX_CHARS = 10000

# Default HTTP settings
DEFAULT_TIMEOUT = 15.0
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_MAX_SIZE_MB = 5.0

# Allowed URL schemes
_ALLOWED_SCHEMES = {"http", "https"}

# Blocked cloud metadata hostnames (case-insensitive comparison)
_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.google.com",
}

# Private / reserved IPv4 networks
_BLOCKED_IPV4_NETWORKS = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
]

# Private / reserved IPv6 networks
_BLOCKED_IPV6_NETWORKS = [
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
]


def _is_blocked_ip(addr: str) -> bool:
    """Return True if *addr* falls within a blocked IP range."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _BLOCKED_IPV4_NETWORKS)
    if isinstance(ip, ipaddress.IPv6Address):
        # Also check IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return any(mapped in net for net in _BLOCKED_IPV4_NETWORKS)
        return any(ip in net for net in _BLOCKED_IPV6_NETWORKS)
    return False


def _validate_url(url: str) -> str | None:
    """Parse *url* and return an error message if it should be blocked.

    Checks:
    - scheme must be http or https
    - hostname must not be a blocked cloud metadata host
    - hostname that is a literal IP must not be in a private range
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "Blocked: unable to parse URL"

    # Scheme check
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"Blocked: scheme '{parsed.scheme}' is not allowed (only http/https)"

    hostname = parsed.hostname
    if not hostname:
        return "Blocked: URL has no hostname"

    # Cloud metadata hostname check
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        return f"Blocked: hostname '{hostname}' is a known cloud metadata endpoint"

    # If the hostname is a literal IP, check it immediately
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_blocked_ip(str(ip)):
            return "Blocked: URL points to a private/internal IP address"
    except ValueError:
        pass  # Not a literal IP — will be checked after DNS resolution

    return None


def _resolve_and_validate(url: str) -> str | None:
    """Resolve the hostname via DNS and return an error if the IP is blocked.

    This prevents DNS rebinding: even if the hostname looks fine, the
    resolved address must not point to a private/internal range.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return "Blocked: URL has no hostname"

    # Skip DNS check for literal IPs (already validated in _validate_url)
    try:
        ipaddress.ip_address(hostname)
        return None
    except ValueError:
        pass

    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"Blocked: DNS resolution failed for '{hostname}'"

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = str(sockaddr[0])
        if _is_blocked_ip(ip_str):
            return "Blocked: URL resolves to a private/internal IP address"

    return None


def fetch_url(
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_size_mb: float = DEFAULT_MAX_SIZE_MB,
) -> dict:
    """Fetch a URL and extract its text content.

    Args:
        url: URL to fetch.
        max_chars: Maximum characters to return from extracted text.
        timeout: Total request timeout in seconds (hard limit: 120s).
        connect_timeout: Connection timeout in seconds (hard limit: 120s).
        max_redirects: Maximum number of redirects to follow.
        max_size_mb: Maximum response size in MB.
    """
    # --- SSRF protection ---
    error = _validate_url(url)
    if error:
        return {"url": url, "error": error}

    error = _resolve_and_validate(url)
    if error:
        return {"url": url, "error": error}

    with requests.Session() as session:
        session.max_redirects = max_redirects
        return _fetch_with_session(
            session, url, max_chars, timeout, connect_timeout, max_redirects, max_size_mb
        )


def _fetch_with_session(
    session: requests.Session,
    url: str,
    max_chars: int,
    timeout: float,
    connect_timeout: float,
    max_redirects: int,
    max_size_mb: float,
) -> dict:
    """Perform the fetch using an already-configured session."""
    try:
        resp = session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=(connect_timeout, timeout),
            allow_redirects=True,
            stream=True,
        )
    except requests.exceptions.ConnectionError:
        return {
            "url": url,
            "error": f"Connection failed: could not connect to {url}",
        }
    except requests.exceptions.Timeout:
        return {
            "url": url,
            "error": (f"Request timed out after {timeout}s (connect timeout: {connect_timeout}s)"),
        }
    except requests.exceptions.TooManyRedirects:
        return {
            "url": url,
            "error": f"Too many redirects (limit: {max_redirects})",
        }

    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        resp.close()
        return {
            "url": url,
            "error": f"HTTP {resp.status_code}: {e}",
        }

    # Check response size before downloading body
    max_bytes = int(max_size_mb * 1024 * 1024)
    content_length = resp.headers.get("Content-Length")
    if content_length and int(content_length) > max_bytes:
        resp.close()
        return {
            "url": url,
            "error": (f"Response too large: {int(content_length)} bytes (limit: {max_size_mb} MB)"),
        }

    # Read body in chunks, enforcing size limit
    chunks = []
    downloaded = 0
    for chunk in resp.iter_content(chunk_size=65536):
        downloaded += len(chunk)
        if downloaded > max_bytes:
            resp.close()
            return {
                "url": url,
                "error": (f"Response too large: >{downloaded} bytes (limit: {max_size_mb} MB)"),
            }
        chunks.append(chunk)
    resp.close()
    body = b"".join(chunks)

    content_type = resp.headers.get("Content-Type", "")
    encoding = resp.encoding or "utf-8"
    text_body = body.decode(encoding, errors="replace")

    if "text/html" in content_type or "application/xhtml" in content_type:
        soup = BeautifulSoup(text_body, "html.parser")

        # Remove script and style elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        text = soup.get_text(separator="\n", strip=True)
    elif "text/" in content_type or "application/json" in content_type:
        title = ""
        text = text_body
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
