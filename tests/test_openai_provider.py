"""Tests for the OpenAI provider."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from creel.providers.base import LLMMessage, TextBlock, ToolUseBlock
from creel.providers.openai import (
    OpenAIProvider,
    _convert_messages_to_openai,
    _convert_response,
    _convert_tools_to_openai,
)

# -- Tool format conversion --


class TestConvertTools:
    def test_basic_tool(self):
        tools = [
            {
                "name": "weather",
                "description": "Get weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            }
        ]

        result = _convert_tools_to_openai(tools)

        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "weather"
        assert result[0]["function"]["description"] == "Get weather"
        assert result[0]["function"]["parameters"]["properties"]["location"]["type"] == "string"

    def test_multiple_tools(self):
        tools = [
            {"name": "a", "description": "A", "input_schema": {}},
            {"name": "b", "description": "B", "input_schema": {}},
        ]

        result = _convert_tools_to_openai(tools)
        assert len(result) == 2
        assert result[0]["function"]["name"] == "a"
        assert result[1]["function"]["name"] == "b"

    def test_empty_tools(self):
        assert _convert_tools_to_openai([]) == []


# -- Message format conversion --


class TestConvertMessages:
    def test_simple_text_messages(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]

        result = _convert_messages_to_openai(messages)
        assert result == messages

    def test_text_blocks_in_list(self):
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": "World"}],
            }
        ]

        result = _convert_messages_to_openai(messages)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Hello\nWorld"

    def test_tool_use_blocks(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_123",
                        "name": "weather",
                        "input": {"location": "Denver"},
                    }
                ],
            }
        ]

        result = _convert_messages_to_openai(messages)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] is None
        assert len(result[0]["tool_calls"]) == 1

        tc = result[0]["tool_calls"][0]
        assert tc["id"] == "call_123"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "weather"
        assert json.loads(tc["function"]["arguments"]) == {"location": "Denver"}

    def test_tool_result_blocks(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_123",
                        "content": "Sunny, 72F",
                    }
                ],
            }
        ]

        result = _convert_messages_to_openai(messages)
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "call_123"
        assert result[0]["content"] == "Sunny, 72F"

    def test_mixed_text_and_tool_results(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "result1"},
                    {"type": "text", "text": "Also this"},
                ],
            }
        ]

        result = _convert_messages_to_openai(messages)
        # Tool result first, then text
        assert len(result) == 2
        assert result[0]["role"] == "tool"
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "Also this"

    def test_assistant_with_text_and_tool_use(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check"},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "weather",
                        "input": {"loc": "NY"},
                    },
                ],
            }
        ]

        result = _convert_messages_to_openai(messages)
        assert result[0]["content"] == "Let me check"
        assert len(result[0]["tool_calls"]) == 1


# -- Response conversion --


class TestConvertResponse:
    def test_text_response(self):
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = "Hello world"
        choice.message.tool_calls = None
        choice.finish_reason = "stop"
        response.choices = [choice]
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

        result = _convert_response(response)

        assert isinstance(result, LLMMessage)
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextBlock)
        assert result.content[0].text == "Hello world"
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    def test_tool_call_response(self):
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = None
        tc = MagicMock()
        tc.id = "call_abc"
        tc.function.name = "weather"
        tc.function.arguments = '{"location": "Denver"}'
        choice.message.tool_calls = [tc]
        choice.finish_reason = "tool_calls"
        response.choices = [choice]
        response.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

        result = _convert_response(response)

        assert result.stop_reason == "tool_use"
        assert len(result.content) == 1
        assert isinstance(result.content[0], ToolUseBlock)
        assert result.content[0].name == "weather"
        assert result.content[0].input == {"location": "Denver"}

    def test_text_with_tool_calls(self):
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = "Let me check"
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "search"
        tc.function.arguments = '{"q": "test"}'
        choice.message.tool_calls = [tc]
        choice.finish_reason = "tool_calls"
        response.choices = [choice]
        response.usage = None

        result = _convert_response(response)

        assert len(result.content) == 2
        assert isinstance(result.content[0], TextBlock)
        assert isinstance(result.content[1], ToolUseBlock)

    def test_max_tokens_finish_reason(self):
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = "Truncat"
        choice.message.tool_calls = None
        choice.finish_reason = "length"
        response.choices = [choice]
        response.usage = None

        result = _convert_response(response)
        assert result.stop_reason == "max_tokens"


# -- Provider integration --


_openai_missing = False
try:
    import openai  # noqa: F401
except ImportError:
    _openai_missing = True


@pytest.mark.skipif(_openai_missing, reason="openai package not installed")
class TestOpenAIProvider:
    @patch("creel.providers.openai._get_openai_client")
    def test_create_basic(self, mock_get_client):
        mock_client = MagicMock()
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = "Hello"
        choice.message.tool_calls = None
        choice.finish_reason = "stop"
        response.choices = [choice]
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create.return_value = response
        mock_get_client.return_value = mock_client

        provider = OpenAIProvider()
        result = provider.create(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o",
            max_tokens=100,
        )

        assert isinstance(result, LLMMessage)
        assert result.content[0].text == "Hello"
        mock_client.chat.completions.create.assert_called_once()

    @patch("creel.providers.openai._get_openai_client")
    def test_create_with_system(self, mock_get_client):
        mock_client = MagicMock()
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = "Hello"
        choice.message.tool_calls = None
        choice.finish_reason = "stop"
        response.choices = [choice]
        response.usage = None
        mock_client.chat.completions.create.return_value = response
        mock_get_client.return_value = mock_client

        provider = OpenAIProvider()
        provider.create(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o",
            max_tokens=100,
            system="You are helpful.",
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        # System message should be prepended
        assert call_kwargs["messages"][0]["role"] == "system"
        assert call_kwargs["messages"][0]["content"] == "You are helpful."

    @patch("creel.providers.openai._get_openai_client")
    def test_create_with_tools(self, mock_get_client):
        mock_client = MagicMock()
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = "Hello"
        choice.message.tool_calls = None
        choice.finish_reason = "stop"
        response.choices = [choice]
        response.usage = None
        mock_client.chat.completions.create.return_value = response
        mock_get_client.return_value = mock_client

        provider = OpenAIProvider()
        tools = [{"name": "weather", "description": "Get weather", "input_schema": {}}]
        provider.create(
            messages=[{"role": "user", "content": "Weather?"}],
            model="gpt-4o",
            max_tokens=100,
            tools=tools,
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert "tools" in call_kwargs
        assert call_kwargs["tools"][0]["type"] == "function"

    def test_extract_env_vars(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        provider = OpenAIProvider()
        env = provider.extract_env_vars()
        assert env["OPENAI_API_KEY"] == "sk-test-key"

    def test_extract_env_vars_missing(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        provider = OpenAIProvider()
        env = provider.extract_env_vars()
        assert "OPENAI_API_KEY" not in env
