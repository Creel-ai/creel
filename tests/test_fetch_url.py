"""Tests for URL fetcher executor — HTML extraction and content handling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from executors.fetch_url.executor import fetch_url


def _mock_response(text, content_type="text/html; charset=utf-8", status_code=200):
    """Create a mock requests.get response."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = text
    mock.headers = {"Content-Type": content_type}
    mock.raise_for_status = MagicMock()
    return mock


# --- fetch_url ---


@patch("executors.fetch_url.executor.requests.get")
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


@patch("executors.fetch_url.executor.requests.get")
def test_fetch_url_respects_max_chars(mock_get):
    """fetch_url should truncate content to max_chars."""
    html = "<html><body><p>" + "x" * 500 + "</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com", max_chars=100)

    assert len(result["content"]) <= 100
    assert result["truncated"] is True


@patch("executors.fetch_url.executor.requests.get")
def test_fetch_url_not_truncated(mock_get):
    """fetch_url should set truncated=False when content fits."""
    html = "<html><body><p>Short content</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com", max_chars=10000)
    assert result["truncated"] is False


@patch("executors.fetch_url.executor.requests.get")
def test_fetch_url_plain_text(mock_get):
    """fetch_url should handle plain text content."""
    mock_get.return_value = _mock_response(
        "Just plain text", content_type="text/plain"
    )

    result = fetch_url("https://example.com/file.txt")
    assert "Just plain text" in result["content"]


@patch("executors.fetch_url.executor.requests.get")
def test_fetch_url_json_content(mock_get):
    """fetch_url should handle JSON content."""
    mock_get.return_value = _mock_response(
        '{"key": "value"}', content_type="application/json"
    )

    result = fetch_url("https://api.example.com/data")
    assert '{"key": "value"}' in result["content"]


@patch("executors.fetch_url.executor.requests.get")
def test_fetch_url_unsupported_content_type(mock_get):
    """fetch_url should return a message for unsupported content types."""
    mock_get.return_value = _mock_response(
        b"binary data", content_type="application/pdf"
    )

    result = fetch_url("https://example.com/file.pdf")
    assert "Unsupported content type" in result["content"]


@patch("executors.fetch_url.executor.requests.get")
def test_fetch_url_collapses_blank_lines(mock_get):
    """fetch_url should collapse multiple blank lines."""
    html = "<html><body><p>Line 1</p><br><br><br><p>Line 2</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com")
    # Should not have multiple consecutive blank lines
    assert "\n\n\n" not in result["content"]


@patch("executors.fetch_url.executor.requests.get")
def test_fetch_url_sends_user_agent(mock_get):
    """fetch_url should send a User-Agent header."""
    mock_get.return_value = _mock_response("<html><body>Hi</body></html>")
    fetch_url("https://example.com")

    call_kwargs = mock_get.call_args
    assert "User-Agent" in call_kwargs.kwargs["headers"]
    assert "Creel" in call_kwargs.kwargs["headers"]["User-Agent"]


@patch("executors.fetch_url.executor.requests.get")
def test_fetch_url_no_title(mock_get):
    """fetch_url should handle pages without a title tag."""
    html = "<html><body><p>No title here</p></body></html>"
    mock_get.return_value = _mock_response(html)

    result = fetch_url("https://example.com")
    assert result["title"] == ""
