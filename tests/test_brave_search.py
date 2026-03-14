"""Tests for Brave Search executor — API interaction and response parsing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from executors.brave_search.executor import search


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    """Set a test API key for all tests."""
    monkeypatch.setenv("BRAVE_API_KEY", "test-key-123")


def _mock_response(results):
    """Create a mock requests.get response with web results."""
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
        "web": {
            "results": results,
        }
    }
    mock.raise_for_status = MagicMock()
    return mock


# --- search ---


@patch("executors.brave_search.executor.requests.get")
def test_search_parses_results(mock_get):
    """search should parse title, url, and snippet from results."""
    mock_get.return_value = _mock_response(
        [
            {
                "title": "Python Tutorial",
                "url": "https://example.com/python",
                "description": "Learn Python",
            },
            {
                "title": "Async IO",
                "url": "https://example.com/async",
                "description": "Async tutorial",
            },
        ]
    )

    results = search("python tutorial")

    assert len(results) == 2
    assert results[0]["title"] == "Python Tutorial"
    assert results[0]["url"] == "https://example.com/python"
    assert results[0]["snippet"] == "Learn Python"


@patch("executors.brave_search.executor.requests.get")
def test_search_sends_correct_headers(mock_get):
    """search should include API key in headers."""
    mock_get.return_value = _mock_response([])
    search("test query")

    call_kwargs = mock_get.call_args
    assert call_kwargs.kwargs["headers"]["X-Subscription-Token"] == "test-key-123"
    assert call_kwargs.kwargs["params"]["q"] == "test query"


@patch("executors.brave_search.executor.requests.get")
def test_search_respects_count(mock_get):
    """search should pass count parameter to API."""
    mock_get.return_value = _mock_response([])
    search("test", count=10)

    call_kwargs = mock_get.call_args
    assert call_kwargs.kwargs["params"]["count"] == 10


@patch("executors.brave_search.executor.requests.get")
def test_search_clamps_count(mock_get):
    """search should clamp count between 1 and 20."""
    mock_get.return_value = _mock_response([])

    search("test", count=50)
    assert mock_get.call_args.kwargs["params"]["count"] == 20

    search("test", count=0)
    assert mock_get.call_args.kwargs["params"]["count"] == 1


@patch("executors.brave_search.executor.requests.get")
def test_search_empty_results(mock_get):
    """search should return empty list when no results."""
    mock_get.return_value = _mock_response([])
    results = search("obscure query")
    assert results == []


def test_search_missing_api_key(monkeypatch):
    """search should raise when BRAVE_API_KEY is not set."""
    monkeypatch.delenv("BRAVE_API_KEY")
    with pytest.raises(RuntimeError, match="BRAVE_API_KEY"):
        search("test")


@patch("executors.brave_search.executor.requests.get")
def test_search_handles_missing_fields(mock_get):
    """search should handle results with missing optional fields."""
    mock_get.return_value = _mock_response(
        [
            {"title": "Partial", "url": "https://example.com"},
        ]
    )
    results = search("test")
    assert len(results) == 1
    assert results[0]["snippet"] == ""


# --- timeout tests ---


@patch("executors.brave_search.executor.requests.get")
def test_search_passes_custom_timeouts(mock_get):
    """search should pass connect and read timeouts to requests."""
    mock_get.return_value = _mock_response([])
    search("test", timeout=30, connect_timeout=10)

    call_kwargs = mock_get.call_args
    assert call_kwargs.kwargs["timeout"] == (10, 30)


@patch("executors.brave_search.executor.requests.get")
def test_search_timeout_error_raises_runtime_error(mock_get):
    """search should raise RuntimeError with clear message on timeout."""
    mock_get.side_effect = requests.exceptions.Timeout("timed out")

    with pytest.raises(RuntimeError, match="timed out"):
        search("test", timeout=5, connect_timeout=2)


@patch("executors.brave_search.executor.requests.get")
def test_search_connection_error_raises_runtime_error(mock_get):
    """search should raise RuntimeError with clear message on connection failure."""
    mock_get.side_effect = requests.exceptions.ConnectionError("refused")

    with pytest.raises(RuntimeError, match="Connection failed"):
        search("test")
