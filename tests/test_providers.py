"""Tests for the provider abstraction layer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creel.providers import (
    LLMMessage,
    TextBlock,
    ToolUseBlock,
    Usage,
    _resolve_model_name,
    _resolve_provider_name,
    build_provider,
    get_provider,
)
from creel.providers.base import LLMProvider

# -- Model string parsing --


class TestParseModelString:
    def test_with_provider_prefix(self):
        provider, model = LLMProvider.parse_model_string("anthropic/claude-sonnet-4-6")
        assert provider == "anthropic"
        assert model == "claude-sonnet-4-6"

    def test_without_prefix(self):
        provider, model = LLMProvider.parse_model_string("claude-sonnet-4-6")
        assert provider is None
        assert model == "claude-sonnet-4-6"

    def test_openai_prefix(self):
        provider, model = LLMProvider.parse_model_string("openai/gpt-4o")
        assert provider == "openai"
        assert model == "gpt-4o"

    def test_bedrock_with_dots(self):
        provider, model = LLMProvider.parse_model_string(
            "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
        )
        assert provider == "bedrock"
        assert model == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def test_ollama_prefix(self):
        provider, model = LLMProvider.parse_model_string("ollama/llama3.3")
        assert provider == "ollama"
        assert model == "llama3.3"


class TestResolveProviderName:
    def test_prefix_overrides_config(self):
        assert _resolve_provider_name("openai/gpt-4o", "anthropic") == "openai"

    def test_falls_back_to_config(self):
        assert _resolve_provider_name("gpt-4o", "openai") == "openai"

    def test_default_anthropic(self):
        assert _resolve_provider_name("claude-sonnet-4-6", "anthropic") == "anthropic"


class TestResolveModelName:
    def test_strips_prefix(self):
        assert _resolve_model_name("openai/gpt-4o") == "gpt-4o"

    def test_no_prefix(self):
        assert _resolve_model_name("claude-sonnet-4-6") == "claude-sonnet-4-6"


# -- Factory --


class TestBuildProvider:
    def test_anthropic(self):
        provider = build_provider("anthropic")
        from creel.providers.anthropic import AnthropicProvider

        assert isinstance(provider, AnthropicProvider)

    def test_anthropic_case_insensitive(self):
        provider = build_provider("Anthropic")
        from creel.providers.anthropic import AnthropicProvider

        assert isinstance(provider, AnthropicProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            build_provider("unknown_provider")


class TestGetProvider:
    def test_default_anthropic(self):
        provider = get_provider()
        from creel.providers.anthropic import AnthropicProvider

        assert isinstance(provider, AnthropicProvider)

    def test_model_prefix_overrides(self):
        """Provider prefix in model string should override the default."""
        provider = get_provider(provider="anthropic", model="bedrock/some-model")
        from creel.providers.bedrock import BedrockProvider

        assert isinstance(provider, BedrockProvider)

    def test_explicit_provider(self):
        provider = get_provider(provider="anthropic", model="claude-sonnet-4-6")
        from creel.providers.anthropic import AnthropicProvider

        assert isinstance(provider, AnthropicProvider)


# -- Unified types --


class TestLLMMessage:
    def test_default_values(self):
        msg = LLMMessage()
        assert msg.content == []
        assert msg.stop_reason == "end_turn"
        assert msg.usage is None

    def test_with_text_block(self):
        msg = LLMMessage(
            content=[TextBlock(text="Hello")],
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
        )
        assert msg.content[0].type == "text"
        assert msg.content[0].text == "Hello"
        assert msg.usage.input_tokens == 10

    def test_with_tool_use_block(self):
        msg = LLMMessage(
            content=[
                ToolUseBlock(
                    id="toolu_1",
                    name="weather",
                    input={"location": "Denver"},
                )
            ],
            stop_reason="tool_use",
        )
        block = msg.content[0]
        assert block.type == "tool_use"
        assert block.name == "weather"
        assert block.input == {"location": "Denver"}

    def test_mixed_content(self):
        msg = LLMMessage(
            content=[
                TextBlock(text="Let me check"),
                ToolUseBlock(id="t1", name="search", input={"q": "test"}),
            ],
        )
        assert len(msg.content) == 2
        assert msg.content[0].type == "text"
        assert msg.content[1].type == "tool_use"


# -- Anthropic provider --


class TestAnthropicProvider:
    @patch("creel.providers.anthropic._get_client")
    def test_create_returns_llm_message(self, mock_get_client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = MagicMock()
        mock_resp = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = "Hi"
        mock_resp.content = [block]
        mock_resp.stop_reason = "end_turn"
        mock_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_client.messages.create.return_value = mock_resp
        mock_get_client.return_value = mock_client

        from creel.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider()
        result = provider.create(
            messages=[{"role": "user", "content": "Hello"}],
            model="claude-sonnet-4-6",
            max_tokens=100,
        )

        assert isinstance(result, LLMMessage)
        assert result.content[0].text == "Hi"
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 10

    @patch("creel.providers.anthropic._get_client")
    def test_create_with_tool_use(self, mock_get_client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = MagicMock()
        mock_resp = MagicMock()

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Let me check"

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "toolu_1"
        tool_block.name = "weather"
        tool_block.input = {"location": "Denver"}

        mock_resp.content = [text_block, tool_block]
        mock_resp.stop_reason = "tool_use"
        mock_resp.usage = MagicMock(input_tokens=20, output_tokens=10)
        mock_client.messages.create.return_value = mock_resp
        mock_get_client.return_value = mock_client

        from creel.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider()
        result = provider.create(
            messages=[{"role": "user", "content": "Weather?"}],
            model="claude-sonnet-4-6",
            max_tokens=100,
        )

        assert len(result.content) == 2
        assert isinstance(result.content[0], TextBlock)
        assert isinstance(result.content[1], ToolUseBlock)
        assert result.content[1].name == "weather"
        assert result.content[1].input == {"location": "Denver"}

    def test_extract_env_vars(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

        from creel.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider()
        env = provider.extract_env_vars()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "token"
        assert env["ANTHROPIC_API_KEY"] == "key"

    @patch("creel.providers.anthropic._get_client")
    def test_api_error_wrapping(self, mock_get_client, monkeypatch):
        """Anthropic SDK errors should be wrapped as LLMProviderError subtypes."""
        import anthropic

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {}
        mock_client.messages.create.side_effect = anthropic.APIStatusError(
            message="rate limited", response=resp, body=None
        )
        mock_get_client.return_value = mock_client

        from creel.providers import LLMRateLimitError
        from creel.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider()
        with pytest.raises(LLMRateLimitError):
            provider.create(
                messages=[{"role": "user", "content": "Hi"}],
                model="claude-sonnet-4-6",
                max_tokens=100,
            )
