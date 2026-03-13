"""Tests for URL fetcher executor — HTML extraction and content handling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from executors.fetch_url.executor import fetch_url


def _mock_response(text, content_type="text/html; charset=utf-8", status_code=200):
    """Create a mock requests.get response."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = text
    mock.headers = {"Content-Type": content_type}
    mock.content = text.encode("utf-8") if isinstance(text, str) else text
    mock.raise_for_status = MagicMock()
    return mock


# --- fetch_url ---


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_extracts_html(mock_get):
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
def test_fetch_url_respects_max_chars(mock_get):
    """fetch_url should truncate content to max_chars."""
    html = "<html><body><p>" + "x" * 500 + "</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com", max_chars=100)

    assert len(result["content"]) <= 100
    assert result["truncated"] is True


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_not_truncated(mock_get):
    """fetch_url should set truncated=False when content fits."""
    html = "<html><body><p>Short content</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com", max_chars=10000)
    assert result["truncated"] is False


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_plain_text(mock_get):
    """fetch_url should handle plain text content."""
    mock_get.return_value = _mock_response("Just plain text", content_type="text/plain")

    result = fetch_url("https://example.com/file.txt")
    assert "Just plain text" in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_json_content(mock_get):
    """fetch_url should handle JSON content."""
    mock_get.return_value = _mock_response('{"key": "value"}', content_type="application/json")

    result = fetch_url("https://api.example.com/data")
    assert '{"key": "value"}' in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_unsupported_content_type(mock_get):
    """fetch_url should return a message for unsupported content types."""
    mock_get.return_value = _mock_response(b"binary data", content_type="application/pdf")

    result = fetch_url("https://example.com/file.pdf")
    assert "Unsupported content type" in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_collapses_blank_lines(mock_get):
    """fetch_url should collapse multiple blank lines."""
    html = "<html><body><p>Line 1</p><br><br><br><p>Line 2</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com")
    # Should not have multiple consecutive blank lines
    assert "\n\n\n" not in result["content"]


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_sends_user_agent(mock_get):
    """fetch_url should send a User-Agent header."""
    mock_get.return_value = _mock_response("<html><body>Hi</body></html>")
    fetch_url("https://example.com")

    call_args = mock_get.call_args
    headers = call_args.kwargs.get("headers") or call_args[1].get("headers", {})
    assert "User-Agent" in headers
    assert "Creel" in headers["User-Agent"]


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_no_title(mock_get):
    """fetch_url should handle pages without a title tag."""
    html = "<html><body><p>No title here</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com")
    assert result["title"] == ""


# --- timeout and limit tests ---


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_passes_custom_timeouts(mock_get):
    """fetch_url should pass connect and read timeouts to requests."""
    mock_get.return_value = _mock_response("<html><body>Hi</body></html>")
    fetch_url("https://example.com", timeout=30, connect_timeout=10)

    call_kwargs = mock_get.call_args
    assert call_kwargs.kwargs["timeout"] == (10, 30)


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_timeout_error_returns_message(mock_get):
    """fetch_url should return a clear error dict on timeout."""
    mock_get.side_effect = requests.exceptions.Timeout("timed out")

    result = fetch_url("https://slow.example.com", timeout=5, connect_timeout=2)

    assert "error" in result
    assert "timed out" in result["error"]
    assert "5" in result["error"]


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_connection_error_returns_message(mock_get):
    """fetch_url should return a clear error dict on connection failure."""
    mock_get.side_effect = requests.exceptions.ConnectionError("refused")

    result = fetch_url("https://down.example.com")

    assert "error" in result
    assert "Connection failed" in result["error"]


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_too_many_redirects(mock_get):
    """fetch_url should return a clear error on too many redirects."""
    mock_get.side_effect = requests.exceptions.TooManyRedirects("too many")

    result = fetch_url("https://loop.example.com", max_redirects=3)

    assert "error" in result
    assert "redirect" in result["error"].lower()


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_response_too_large_by_content_length(mock_get):
    """fetch_url should reject responses exceeding max_size_mb via Content-Length."""
    mock = _mock_response("<html><body>data</body></html>")
    mock.headers["Content-Length"] = str(10 * 1024 * 1024)  # 10 MB
    mock.content = b"x" * 100  # actual content small, but header says large
    mock_get.return_value = mock

    result = fetch_url("https://example.com", max_size_mb=1.0)

    assert "error" in result
    assert "too large" in result["error"].lower()


@patch("executors.fetch_url.executor.requests.Session.get")
def test_fetch_url_response_too_large_by_actual_content(mock_get):
    """fetch_url should reject responses exceeding max_size_mb by actual size."""
    mock = _mock_response("<html><body>data</body></html>")
    mock.headers.pop("Content-Length", None)
    mock.content = b"x" * (2 * 1024 * 1024)  # 2 MB
    mock_get.return_value = mock

    result = fetch_url("https://example.com", max_size_mb=1.0)

    assert "error" in result
    assert "too large" in result["error"].lower()
