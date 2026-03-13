"""Tests for failover config, model override, and health checks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from creel.models import LLMConfig, SessionConfig
from creel.providers.base import LLMProvider

# -- LLMConfig.fallback --


class TestLLMConfigFallback:
    def test_default_empty(self):
        config = LLMConfig()
        assert config.fallback == []

    def test_fallback_list(self):
        config = LLMConfig(fallback=["openai/gpt-4o", "ollama/llama3.2", "gemini/gemini-2.0-flash"])
        assert len(config.fallback) == 3
        assert config.fallback[0] == "openai/gpt-4o"
        assert config.fallback[1] == "ollama/llama3.2"
        assert config.fallback[2] == "gemini/gemini-2.0-flash"

    def test_from_dict(self):
        config = LLMConfig(
            **{
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "fallback": ["openai/gpt-4o"],
            }
        )
        assert config.fallback == ["openai/gpt-4o"]

    def test_serialization_roundtrip(self):
        config = LLMConfig(fallback=["openai/gpt-4o", "ollama/llama3.2"])
        dumped = config.model_dump()
        assert dumped["fallback"] == ["openai/gpt-4o", "ollama/llama3.2"]
        restored = LLMConfig(**dumped)
        assert restored.fallback == config.fallback


# -- SessionConfig.model_override --


class TestSessionConfigModelOverride:
    def test_default_none(self):
        config = SessionConfig()
        assert config.model_override is None

    def test_set_override(self):
        config = SessionConfig(model_override="openai/gpt-4o")
        assert config.model_override == "openai/gpt-4o"

    def test_from_dict(self):
        config = SessionConfig(
            **{"sessions_dir": "sessions", "model_override": "gemini/gemini-pro"}
        )
        assert config.model_override == "gemini/gemini-pro"


# -- Health check on all providers --


class TestProviderHealthDefaults:
    def test_base_provider_health_returns_true(self):
        """Default health() on the ABC returns True."""

        class ConcreteProvider(LLMProvider):
            def create(self, **kwargs):
                pass

            def stream(self, **kwargs):
                pass

        p = ConcreteProvider()
        assert p.health() is True


class TestAnthropicHealth:
    @patch("creel.providers.anthropic._get_client")
    def test_health_success(self, mock_get_client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = MagicMock()
        mock_client.models.list.return_value = []
        mock_get_client.return_value = mock_client

        from creel.providers.anthropic import AnthropicProvider

        p = AnthropicProvider()
        assert p.health() is True

    @patch("creel.providers.anthropic._get_client")
    def test_health_failure(self, mock_get_client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = MagicMock()
        mock_client.models.list.side_effect = Exception("connection refused")
        mock_get_client.return_value = mock_client

        from creel.providers.anthropic import AnthropicProvider

        p = AnthropicProvider()
        assert p.health() is False


class TestOpenAIHealth:
    @patch("creel.providers.openai._get_openai_client")
    def test_health_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.models.list.return_value = []
        mock_get_client.return_value = mock_client

        from creel.providers.openai import OpenAIProvider

        p = OpenAIProvider()
        assert p.health() is True

    @patch("creel.providers.openai._get_openai_client")
    def test_health_failure(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.models.list.side_effect = Exception("connection refused")
        mock_get_client.return_value = mock_client

        from creel.providers.openai import OpenAIProvider

        p = OpenAIProvider()
        assert p.health() is False


class TestOllamaHealth:
    @patch("creel.providers.ollama.httpx.get")
    def test_health_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        from creel.providers.ollama import OllamaProvider

        p = OllamaProvider()
        assert p.health() is True

    @patch("creel.providers.ollama.httpx.get")
    def test_health_failure(self, mock_get):
        mock_get.side_effect = Exception("connection refused")

        from creel.providers.ollama import OllamaProvider

        p = OllamaProvider()
        assert p.health() is False


class TestBedrockHealth:
    def test_health_success(self, monkeypatch):
        import sys

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_client.list_foundation_models.return_value = {"modelSummaries": []}
        mock_boto3.client.return_value = mock_client
        monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

        from creel.providers.bedrock import BedrockProvider

        p = BedrockProvider()
        assert p.health() is True

    def test_health_failure_connection_error(self, monkeypatch):
        import sys

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception("connection refused")
        monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

        from creel.providers.bedrock import BedrockProvider

        p = BedrockProvider()
        assert p.health() is False


# -- call_llm with model_override --


class TestCallLLMModelOverride:
    @patch("creel.llm.get_provider_with_fallback")
    def test_model_override_used(self, mock_get_provider):
        from creel.llm import call_llm

        mock_provider = MagicMock()
        mock_provider.create.return_value = MagicMock(
            content=[MagicMock(type="text", text="ok")],
            stop_reason="end_turn",
            usage=None,
        )
        mock_get_provider.return_value = mock_provider

        config = LLMConfig(provider="anthropic", model="claude-sonnet-4-20250514")
        call_llm(
            messages=[{"role": "user", "content": "hi"}],
            config=config,
            model_override="openai/gpt-4o",
        )

        # Verify that get_provider_with_fallback was called with the override model
        call_kwargs = mock_get_provider.call_args[1]
        assert call_kwargs["model"] == "openai/gpt-4o"

    @patch("creel.llm.get_provider_with_fallback")
    def test_no_override_uses_config_model(self, mock_get_provider):
        from creel.llm import call_llm

        mock_provider = MagicMock()
        mock_provider.create.return_value = MagicMock(
            content=[MagicMock(type="text", text="ok")],
            stop_reason="end_turn",
            usage=None,
        )
        mock_get_provider.return_value = mock_provider

        config = LLMConfig(provider="anthropic", model="claude-sonnet-4-20250514")
        call_llm(
            messages=[{"role": "user", "content": "hi"}],
            config=config,
        )

        call_kwargs = mock_get_provider.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-4-20250514"

    @patch("creel.llm.get_provider_with_fallback")
    def test_fallback_passed_to_provider(self, mock_get_provider):
        from creel.llm import call_llm

        mock_provider = MagicMock()
        mock_provider.create.return_value = MagicMock(
            content=[MagicMock(type="text", text="ok")],
            stop_reason="end_turn",
            usage=None,
        )
        mock_get_provider.return_value = mock_provider

        config = LLMConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            fallback=["openai/gpt-4o", "ollama/llama3.2"],
        )
        call_llm(
            messages=[{"role": "user", "content": "hi"}],
            config=config,
        )

        call_kwargs = mock_get_provider.call_args[1]
        assert call_kwargs["fallback"] == ["openai/gpt-4o", "ollama/llama3.2"]
