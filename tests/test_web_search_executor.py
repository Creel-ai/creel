"""Tests for the web search executor."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from executors.web_search.executor import search_web


class TestSearchWeb:
    """Tests for search_web function with mocked API."""

    @patch("executors.web_search.executor.requests.get")
    def test_successful_search(self, mock_get: MagicMock) -> None:
        """Test successful web search with results."""
        # Mock response data
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {
                        "title": "Example Result 1",
                        "url": "https://example.com/1",
                        "description": "First result snippet",
                        "age": "2024-02-01T12:00:00",
                        "language": "en"
                    },
                    {
                        "title": "Example Result 2", 
                        "url": "https://example.com/2",
                        "description": "Second result snippet",
                        "age": "2024-02-02T12:00:00",
                        "language": "en"
                    }
                ]
            }
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # Set up environment
        with patch.dict(os.environ, {"BRAVE_API_KEY": "test-api-key"}):
            result = search_web("test query", 5)

        # Verify request was made correctly
        mock_get.assert_called_once_with(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "X-Subscription-Token": "test-api-key",
                "Accept": "application/json"
            },
            params={"q": "test query", "count": 5},
            timeout=15.0
        )

        # Verify response format
        assert result["query"] == "test query"
        assert result["count_requested"] == 5
        assert result["count_returned"] == 2
        assert len(result["results"]) == 2
        
        # Verify first result
        first_result = result["results"][0]
        assert first_result["title"] == "Example Result 1"
        assert first_result["url"] == "https://example.com/1"
        assert first_result["snippet"] == "First result snippet"
        assert first_result["age"] == "2024-02-01T12:00:00"
        assert first_result["language"] == "en"

    @patch("executors.web_search.executor.requests.get")
    def test_empty_results(self, mock_get: MagicMock) -> None:
        """Test search with no results."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"web": {"results": []}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.dict(os.environ, {"BRAVE_API_KEY": "test-api-key"}):
            result = search_web("very specific query with no results", 3)

        assert result["query"] == "very specific query with no results"
        assert result["count_requested"] == 3
        assert result["count_returned"] == 0
        assert result["results"] == []

    @patch("executors.web_search.executor.requests.get")
    def test_partial_result_fields(self, mock_get: MagicMock) -> None:
        """Test handling of results with missing optional fields."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {
                        "title": "Minimal Result",
                        "url": "https://example.com"
                        # description, age, and language missing
                    }
                ]
            }
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.dict(os.environ, {"BRAVE_API_KEY": "test-api-key"}):
            result = search_web("test", 1)

        assert len(result["results"]) == 1
        first_result = result["results"][0]
        assert first_result["title"] == "Minimal Result"
        assert first_result["url"] == "https://example.com"
        assert first_result["snippet"] == ""  # Default to empty string
        assert first_result["age"] == ""
        assert first_result["language"] == ""

    def test_missing_api_key(self) -> None:
        """Test error when BRAVE_API_KEY is not set."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="BRAVE_API_KEY environment variable is required"):
                search_web("test query")

    @patch("executors.web_search.executor.requests.get")
    def test_api_request_error(self, mock_get: MagicMock) -> None:
        """Test handling of API request errors."""
        mock_get.side_effect = requests.exceptions.RequestException("Connection failed")

        with patch.dict(os.environ, {"BRAVE_API_KEY": "test-api-key"}):
            with pytest.raises(RuntimeError, match="Web search request failed: Connection failed"):
                search_web("test query")

    @patch("executors.web_search.executor.requests.get")
    def test_http_error(self, mock_get: MagicMock) -> None:
        """Test handling of HTTP error responses."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("401 Unauthorized")
        mock_get.return_value = mock_response

        with patch.dict(os.environ, {"BRAVE_API_KEY": "invalid-key"}):
            with pytest.raises(RuntimeError, match="Web search request failed"):
                search_web("test query")

    @patch("executors.web_search.executor.requests.get")
    def test_malformed_response(self, mock_get: MagicMock) -> None:
        """Test handling of malformed API responses."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"unexpected": "format"}  # Missing 'web' key
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.dict(os.environ, {"BRAVE_API_KEY": "test-api-key"}):
            # This should not raise an exception, just return empty results
            result = search_web("test query")
            assert result["results"] == []

    @patch("executors.web_search.executor.requests.get")
    def test_count_limiting(self, mock_get: MagicMock) -> None:
        """Test that count is limited to 20 (Brave API limit)."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"web": {"results": []}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.dict(os.environ, {"BRAVE_API_KEY": "test-api-key"}):
            search_web("test query", 100)

        # Verify count was capped at 20
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert kwargs["params"]["count"] == 20

    @patch("executors.web_search.executor.requests.get")
    def test_default_count(self, mock_get: MagicMock) -> None:
        """Test default count parameter."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"web": {"results": []}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.dict(os.environ, {"BRAVE_API_KEY": "test-api-key"}):
            result = search_web("test query")  # No count specified

        # Verify default count was used
        assert result["count_requested"] == 5
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert kwargs["params"]["count"] == 5