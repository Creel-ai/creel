"""Tests for the Bedrock provider."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

from creel.providers.base import LLMMessage, TextBlock, ToolUseBlock
from creel.providers.bedrock import BedrockProvider, _convert_bedrock_response


class TestConvertBedrockResponse:
    def test_text_response(self):
        body = {
            "content": [{"type": "text", "text": "Hello world"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = _convert_bedrock_response(body)

        assert isinstance(result, LLMMessage)
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextBlock)
        assert result.content[0].text == "Hello world"
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 10

    def test_tool_use_response(self):
        body = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "weather",
                    "input": {"location": "Denver"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
        result = _convert_bedrock_response(body)

        assert result.stop_reason == "tool_use"
        assert isinstance(result.content[0], ToolUseBlock)
        assert result.content[0].name == "weather"

    def test_no_usage(self):
        body = {
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
        }
        result = _convert_bedrock_response(body)
        assert result.usage is None


class TestBedrockProvider:
    @patch("creel.providers.bedrock._get_bedrock_client")
    def test_create_basic(self, mock_get_client):
        response_body = {
            "content": [{"type": "text", "text": "Hello from Bedrock"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 15, "output_tokens": 8},
        }

        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {
            "body": BytesIO(json.dumps(response_body).encode()),
        }
        mock_get_client.return_value = mock_client

        provider = BedrockProvider(region="us-east-1")
        result = provider.create(
            messages=[{"role": "user", "content": "Hi"}],
            model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            max_tokens=100,
        )

        assert isinstance(result, LLMMessage)
        assert result.content[0].text == "Hello from Bedrock"

        # Verify invoke_model was called with correct modelId
        call_kwargs = mock_client.invoke_model.call_args[1]
        assert call_kwargs["modelId"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    @patch("creel.providers.bedrock._get_bedrock_client")
    def test_create_with_system_and_tools(self, mock_get_client):
        response_body = {
            "content": [{"type": "text", "text": "Ok"}],
            "stop_reason": "end_turn",
        }

        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {
            "body": BytesIO(json.dumps(response_body).encode()),
        }
        mock_get_client.return_value = mock_client

        provider = BedrockProvider()
        tools = [{"name": "weather", "description": "Get weather", "input_schema": {}}]
        provider.create(
            messages=[{"role": "user", "content": "Weather?"}],
            model="anthropic.claude-3-5-sonnet",
            max_tokens=100,
            system="You are helpful.",
            tools=tools,
        )

        # Verify body contains system and tools
        call_kwargs = mock_client.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["system"] == "You are helpful."
        assert body["tools"] == tools
        assert body["anthropic_version"] == "bedrock-2023-05-31"

    def test_extract_env_vars(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA...")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)

        provider = BedrockProvider(region="us-west-2")
        env = provider.extract_env_vars()
        assert env["AWS_ACCESS_KEY_ID"] == "AKIA..."
        assert env["AWS_SECRET_ACCESS_KEY"] == "secret"
        assert env["AWS_REGION"] == "us-west-2"
