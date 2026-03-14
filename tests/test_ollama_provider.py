"""Tests for the Ollama provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creel.providers.base import LLMMessage, LLMProviderError, ToolUseBlock
from creel.providers.ollama import OllamaProvider


class TestOllamaProvider:
    def test_default_base_url(self):
        provider = OllamaProvider()
        assert provider._api_base == "http://localhost:11434"

    def test_custom_base_url(self):
        provider = OllamaProvider(api_base="http://myhost:9999")
        assert provider._api_base == "http://myhost:9999"

    def test_trailing_slash_stripped(self):
        provider = OllamaProvider(api_base="http://myhost:9999/")
        assert provider._api_base == "http://myhost:9999"

    @patch("creel.providers.ollama.httpx.post")
    def test_create_basic(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {"content": "Hello from Ollama", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_post.return_value = mock_response

        provider = OllamaProvider()
        result = provider.create(
            messages=[{"role": "user", "content": "Hi"}],
            model="llama3.3",
            max_tokens=100,
        )

        assert isinstance(result, LLMMessage)
        assert result.content[0].text == "Hello from Ollama"
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 10

        # Verify the correct URL was called
        call_args = mock_post.call_args
        assert call_args[0][0] == "http://localhost:11434/v1/chat/completions"

    @patch("creel.providers.ollama.httpx.post")
    def test_create_with_system(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {"content": "Hi", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
        }
        mock_post.return_value = mock_response

        provider = OllamaProvider()
        provider.create(
            messages=[{"role": "user", "content": "Hi"}],
            model="llama3.3",
            max_tokens=100,
            system="You are helpful.",
        )

        # Verify system message was prepended
        call_kwargs = mock_post.call_args[1]
        messages = call_kwargs["json"]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are helpful."

    @patch("creel.providers.ollama.httpx.post")
    def test_create_with_tool_response(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"location": "Denver"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        mock_post.return_value = mock_response

        provider = OllamaProvider()
        result = provider.create(
            messages=[{"role": "user", "content": "Weather?"}],
            model="llama3.3",
            max_tokens=100,
        )

        assert result.stop_reason == "tool_use"
        assert len(result.content) == 1
        assert isinstance(result.content[0], ToolUseBlock)
        assert result.content[0].name == "weather"
        assert result.content[0].input == {"location": "Denver"}

    @patch("creel.providers.ollama.httpx.post")
    def test_connection_error(self, mock_post):
        import httpx

        mock_post.side_effect = httpx.ConnectError("Connection refused")

        provider = OllamaProvider()
        with pytest.raises(LLMProviderError, match="Cannot connect to Ollama"):
            provider.create(
                messages=[{"role": "user", "content": "Hi"}],
                model="llama3.3",
                max_tokens=100,
            )

    def test_extract_env_vars(self):
        provider = OllamaProvider(api_base="http://custom:1234")
        env = provider.extract_env_vars()
        assert env["OLLAMA_HOST"] == "http://custom:1234"

    def test_format_tools(self):
        provider = OllamaProvider()
        tools = [{"name": "search", "description": "Search", "input_schema": {"type": "object"}}]
        result = provider.format_tools(tools)
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search"
