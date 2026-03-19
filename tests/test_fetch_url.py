"""Tests for URL fetcher executor — HTML extraction, content handling, and SSRF protection."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest
import requests

from executors.fetch_url.executor import (
    _is_blocked_ip,
    _resolve_and_validate,
    _validate_url,
    fetch_url,
)


def _mock_response(text, content_type="text/html; charset=utf-8", status_code=200):
    """Create a mock requests.Session.get response (stream=True compatible)."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.headers = {"Content-Type": content_type}
    raw_bytes = text.encode("utf-8") if isinstance(text, str) else text
    mock.encoding = "utf-8"
    mock.iter_content = MagicMock(return_value=iter([raw_bytes]))
    mock.close = MagicMock()
    mock.raise_for_status = MagicMock()
    # Keep .content for back-compat in size tests that override iter_content
    mock.content = raw_bytes
    return mock


def _fake_public_getaddrinfo(host, port, *args, **kwargs):
    """Return a fake public IP for any hostname — used by existing fetch tests."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]


# --- fetch_url: HTML extraction ---


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_extracts_html(_mock_dns, mock_get):
    """fetch_url should extract text content from HTML."""
    html = """
    <html>
    <head><title>Test Page</title></head>
    <body>
        <nav>Navigation</nav>
        <h1>Hello World</h1>
        <p>This is a test paragraph.</p>
        <script>var x = 1;</script>
        <footer>Footer content</footer>
    </body>
    </html>
    """
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com")

    assert result["title"] == "Test Page"
    assert result["url"] == "https://example.com"
    assert "Hello World" in result["content"]
    assert "This is a test paragraph" in result["content"]
    # Script, nav, footer should be removed
    assert "var x = 1" not in result["content"]
    assert "Navigation" not in result["content"]
    assert "Footer content" not in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_respects_max_chars(_mock_dns, mock_get):
    """fetch_url should truncate content to max_chars."""
    html = "<html><body><p>" + "x" * 500 + "</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com", max_chars=100)

    assert len(result["content"]) <= 100
    assert result["truncated"] is True


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_not_truncated(_mock_dns, mock_get):
    """fetch_url should set truncated=False when content fits."""
    html = "<html><body><p>Short content</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com", max_chars=10000)
    assert result["truncated"] is False


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_plain_text(_mock_dns, mock_get):
    """fetch_url should handle plain text content."""
    mock_get.return_value = _mock_response("Just plain text", content_type="text/plain")

    result = fetch_url("https://example.com/file.txt")
    assert "Just plain text" in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_json_content(_mock_dns, mock_get):
    """fetch_url should handle JSON content."""
    mock_get.return_value = _mock_response('{"key": "value"}', content_type="application/json")

    result = fetch_url("https://api.example.com/data")
    assert '{"key": "value"}' in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_unsupported_content_type(_mock_dns, mock_get):
    """fetch_url should return a message for unsupported content types."""
    mock_get.return_value = _mock_response(b"binary data", content_type="application/pdf")

    result = fetch_url("https://example.com/file.pdf")
    assert "Unsupported content type" in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_collapses_blank_lines(_mock_dns, mock_get):
    """fetch_url should collapse multiple blank lines."""
    html = "<html><body><p>Line 1</p><br><br><br><p>Line 2</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com")
    # Should not have multiple consecutive blank lines
    assert "\n\n\n" not in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_sends_user_agent(_mock_dns, mock_get):
    """fetch_url should send a User-Agent header."""
    mock_get.return_value = _mock_response("<html><body>Hi</body></html>")
    fetch_url("https://example.com")

    call_args = mock_get.call_args
    headers = call_args.kwargs.get("headers") or call_args[1].get("headers", {})
    assert "User-Agent" in headers
    assert "Creel" in headers["User-Agent"]


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_no_title(_mock_dns, mock_get):
    """fetch_url should handle pages without a title tag."""
    html = "<html><body><p>No title here</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com")
    assert result["title"] == ""


# --- timeout and limit tests ---


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_passes_custom_timeouts(_mock_dns, mock_get):
    """fetch_url should pass connect and read timeouts to requests."""
    mock_get.return_value = _mock_response("<html><body>Hi</body></html>")
    fetch_url("https://example.com", timeout=30, connect_timeout=10)

    call_kwargs = mock_get.call_args
    assert call_kwargs.kwargs["timeout"] == (10, 30)


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_timeout_error_returns_message(_mock_dns, mock_get):
    """fetch_url should return a clear error dict on timeout."""
    mock_get.side_effect = requests.exceptions.Timeout("timed out")

    result = fetch_url("https://slow.example.com", timeout=5, connect_timeout=2)

    assert "error" in result
    assert "timed out" in result["error"]
    assert "5" in result["error"]


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_connection_error_returns_message(_mock_dns, mock_get):
    """fetch_url should return a clear error dict on connection failure."""
    mock_get.side_effect = requests.exceptions.ConnectionError("refused")

    result = fetch_url("https://down.example.com")

    assert "error" in result
    assert "Connection failed" in result["error"]


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_too_many_redirects(_mock_dns, mock_get):
    """fetch_url should return a clear error on too many redirects."""
    mock_get.side_effect = requests.exceptions.TooManyRedirects("too many")

    result = fetch_url("https://loop.example.com", max_redirects=3)

    assert "error" in result
    assert "redirect" in result["error"].lower()


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_response_too_large_by_content_length(_mock_dns, mock_get):
    """fetch_url should reject responses exceeding max_size_mb via Content-Length."""
    mock = _mock_response("<html><body>data</body></html>")
    mock.headers["Content-Length"] = str(10 * 1024 * 1024)  # 10 MB
    mock.content = b"x" * 100  # actual content small, but header says large
    mock_get.return_value = mock

    result = fetch_url("https://example.com", max_size_mb=1.0)

    assert "error" in result
    assert "too large" in result["error"].lower()


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_response_too_large_by_actual_content(_mock_dns, mock_get):
    """fetch_url should reject responses exceeding max_size_mb by actual size."""
    mock = _mock_response("<html><body>data</body></html>")
    mock.headers.pop("Content-Length", None)
    # Simulate streaming 2 MB across multiple chunks
    chunk = b"x" * 65536
    chunks = [chunk] * 32  # 32 * 64KB = 2 MB
    mock.iter_content = MagicMock(return_value=iter(chunks))
    mock_get.return_value = mock

    result = fetch_url("https://example.com", max_size_mb=1.0)

    assert "error" in result
    assert "too large" in result["error"].lower()


@patch("executors.fetch_url.executor.requests.Session.get")
@patch("executors.fetch_url.executor.socket.getaddrinfo", side_effect=_fake_public_getaddrinfo)
def test_fetch_url_http_error_returns_message(_mock_dns, mock_get):
    """fetch_url should return an error dict on HTTP 4xx/5xx responses."""
    mock = _mock_response("<html><body>Not Found</body></html>", status_code=404)
    mock.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Client Error")
    mock_get.return_value = mock

    result = fetch_url("https://example.com/missing")

    assert "error" in result
    assert "404" in result["error"]


# ===========================================================================
# SSRF protection tests
# ===========================================================================


class TestIsBlockedIp:
    """Unit tests for the _is_blocked_ip helper."""

    @pytest.mark.parametrize(
        "ip",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.1.100",
            "127.0.0.1",
            "127.0.0.2",
            "169.254.169.254",  # AWS/Azure metadata
            "169.254.0.1",
        ],
    )
    def test_blocked_ipv4(self, ip):
        assert _is_blocked_ip(ip) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",
            "93.184.216.34",
            "1.1.1.1",
            "203.0.113.1",
        ],
    )
    def test_allowed_ipv4(self, ip):
        assert _is_blocked_ip(ip) is False

    @pytest.mark.parametrize(
        "ip",
        [
            "::1",
            "fc00::1",
            "fdff::1",
            "fe80::1",
        ],
    )
    def test_blocked_ipv6(self, ip):
        assert _is_blocked_ip(ip) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "2607:f8b0:4004:800::200e",  # Google public
        ],
    )
    def test_allowed_ipv6(self, ip):
        assert _is_blocked_ip(ip) is False

    def test_ipv4_mapped_ipv6_loopback(self):
        """::ffff:127.0.0.1 should be blocked (IPv4-mapped loopback)."""
        assert _is_blocked_ip("::ffff:127.0.0.1") is True

    def test_ipv4_mapped_ipv6_private(self):
        """::ffff:10.0.0.1 should be blocked (IPv4-mapped private)."""
        assert _is_blocked_ip("::ffff:10.0.0.1") is True

    def test_ipv4_mapped_ipv6_public(self):
        """::ffff:8.8.8.8 should not be blocked."""
        assert _is_blocked_ip("::ffff:8.8.8.8") is False

    def test_invalid_ip_returns_false(self):
        assert _is_blocked_ip("not-an-ip") is False


class TestValidateUrl:
    """Unit tests for the _validate_url function."""

    def test_allows_https(self):
        assert _validate_url("https://example.com") is None

    def test_allows_http(self):
        assert _validate_url("http://example.com") is None

    def test_blocks_file_scheme(self):
        err = _validate_url("file:///etc/passwd")
        assert err is not None
        assert "scheme" in err.lower()

    def test_blocks_ftp_scheme(self):
        err = _validate_url("ftp://example.com/file")
        assert err is not None
        assert "scheme" in err.lower()

    def test_blocks_gopher_scheme(self):
        err = _validate_url("gopher://evil.com")
        assert err is not None
        assert "scheme" in err.lower()

    def test_blocks_data_scheme(self):
        err = _validate_url("data:text/html,<h1>hi</h1>")
        assert err is not None
        assert "scheme" in err.lower()

    def test_blocks_metadata_google_internal(self):
        err = _validate_url("http://metadata.google.internal/computeMetadata/v1/")
        assert err is not None
        assert "metadata" in err.lower()

    def test_blocks_metadata_google_com(self):
        err = _validate_url("http://metadata.google.com/computeMetadata/v1/")
        assert err is not None
        assert "metadata" in err.lower()

    def test_blocks_literal_private_ip(self):
        err = _validate_url("http://10.0.0.1/admin")
        assert err is not None
        assert "private" in err.lower()

    def test_blocks_literal_loopback(self):
        err = _validate_url("http://127.0.0.1:8080/")
        assert err is not None
        assert "private" in err.lower()

    def test_blocks_literal_link_local(self):
        err = _validate_url("http://169.254.169.254/latest/meta-data/")
        assert err is not None
        assert "private" in err.lower()

    def test_blocks_literal_ipv6_loopback(self):
        err = _validate_url("http://[::1]:8080/")
        assert err is not None
        assert "private" in err.lower()

    def test_allows_public_ip_literal(self):
        assert _validate_url("http://93.184.216.34/") is None

    def test_blocks_no_hostname(self):
        err = _validate_url("http://")
        assert err is not None


class TestResolveAndValidate:
    """Unit tests for the _resolve_and_validate function."""

    @patch("executors.fetch_url.executor.socket.getaddrinfo")
    def test_blocks_dns_resolving_to_private(self, mock_dns):
        """A hostname resolving to 10.x should be blocked."""
        mock_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        err, ips = _resolve_and_validate("https://evil.example.com")
        assert err is not None
        assert "private" in err.lower()
        assert ips == []

    @patch("executors.fetch_url.executor.socket.getaddrinfo")
    def test_blocks_dns_resolving_to_loopback(self, mock_dns):
        """A hostname resolving to 127.0.0.1 should be blocked (DNS rebinding)."""
        mock_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        err, ips = _resolve_and_validate("https://rebind.example.com")
        assert err is not None
        assert "private" in err.lower()
        assert ips == []

    @patch("executors.fetch_url.executor.socket.getaddrinfo")
    def test_blocks_dns_resolving_to_link_local(self, mock_dns):
        """A hostname resolving to 169.254.x should be blocked."""
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
        ]
        err, ips = _resolve_and_validate("https://metadata-trick.example.com")
        assert err is not None
        assert "private" in err.lower()
        assert ips == []

    @patch("executors.fetch_url.executor.socket.getaddrinfo")
    def test_allows_dns_resolving_to_public(self, mock_dns):
        """A hostname resolving to a public IP should pass and return IPs."""
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        err, ips = _resolve_and_validate("https://example.com")
        assert err is None
        assert ips == ["93.184.216.34"]

    @patch("executors.fetch_url.executor.socket.getaddrinfo")
    def test_blocks_if_any_address_is_private(self, mock_dns):
        """If DNS returns mixed public+private, block (any private is bad)."""
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]
        err, ips = _resolve_and_validate("https://mixed.example.com")
        assert err is not None
        assert "private" in err.lower()
        assert ips == []

    @patch(
        "executors.fetch_url.executor.socket.getaddrinfo",
        side_effect=socket.gaierror("Name or service not known"),
    )
    def test_blocks_dns_failure(self, _mock_dns):
        """DNS resolution failure should be blocked."""
        err, ips = _resolve_and_validate("https://doesnotexist.invalid")
        assert err is not None
        assert "DNS" in err
        assert ips == []

    def test_skips_dns_for_literal_ip(self):
        """Literal IPs skip DNS (already validated in _validate_url)."""
        err, ips = _resolve_and_validate("https://93.184.216.34/")
        assert err is None
        assert ips == []  # no DNS resolution needed


class TestFetchUrlSsrfIntegration:
    """Integration tests: fetch_url should block SSRF attempts end-to-end."""

    def test_blocks_private_ip_10(self):
        result = fetch_url("http://10.0.0.1/admin")
        assert "error" in result
        assert "Blocked" in result["error"]

    def test_blocks_private_ip_172(self):
        result = fetch_url("http://172.16.0.1/")
        assert "error" in result
        assert "Blocked" in result["error"]

    def test_blocks_private_ip_192(self):
        result = fetch_url("http://192.168.1.1/")
        assert "error" in result
        assert "Blocked" in result["error"]

    def test_blocks_loopback(self):
        result = fetch_url("http://127.0.0.1:8080/secret")
        assert "error" in result
        assert "Blocked" in result["error"]

    def test_blocks_aws_metadata(self):
        result = fetch_url("http://169.254.169.254/latest/meta-data/")
        assert "error" in result
        assert "Blocked" in result["error"]

    def test_blocks_gce_metadata(self):
        result = fetch_url("http://metadata.google.internal/computeMetadata/v1/")
        assert "error" in result
        assert "Blocked" in result["error"]

    def test_blocks_file_scheme(self):
        result = fetch_url("file:///etc/passwd")
        assert "error" in result
        assert "Blocked" in result["error"]
        assert "scheme" in result["error"].lower()

    def test_blocks_data_scheme(self):
        result = fetch_url("data:text/html,<script>alert(1)</script>")
        assert "error" in result
        assert "Blocked" in result["error"]

    @patch("executors.fetch_url.executor.socket.getaddrinfo")
    def test_blocks_dns_rebinding(self, mock_dns):
        """A hostname that resolves to a private IP should be blocked."""
        mock_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        result = fetch_url("https://attacker-controlled.example.com")
        assert "error" in result
        assert "Blocked" in result["error"]
        assert "private" in result["error"].lower()

    def test_blocks_ipv6_loopback(self):
        result = fetch_url("http://[::1]:8080/")
        assert "error" in result
        assert "Blocked" in result["error"]
