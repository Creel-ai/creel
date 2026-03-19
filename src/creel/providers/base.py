"""Provider abstraction — unified types and ABC for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

# --- Unified response types ---


@dataclass
class TextBlock:
    """A text content block in an LLM response."""

    text: str = ""
    type: Literal["text"] = "text"


@dataclass
class ToolUseBlock:
    """A tool_use content block in an LLM response."""

    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    type: Literal["tool_use"] = "tool_use"


ContentBlock = TextBlock | ToolUseBlock


@dataclass
class Usage:
    """Token usage from an LLM response."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMMessage:
    """Provider-agnostic LLM response message."""

    content: list[ContentBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use" | "max_tokens"
    usage: Usage | None = None


# --- Common error types ---


class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMRateLimitError(LLMProviderError):
    """Rate limit error (429)."""


class LLMTransientError(LLMProviderError):
    """Transient server error (500/502/503)."""


class LLMAuthError(LLMProviderError):
    """Authentication/authorization error (401/403)."""


# --- Provider ABC ---


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def create(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
        timeout: float | None = None,
    ) -> LLMMessage:
        """Make a non-streaming LLM call.

        Args:
            messages: Conversation messages in Anthropic format (the internal
                format used throughout Creel).
            model: Model identifier (without provider prefix).
            max_tokens: Maximum output tokens.
            system: Optional system prompt.
            tools: Optional tool definitions in Anthropic format.
            timeout: Optional request timeout in seconds.

        Returns:
            A provider-agnostic LLMMessage.
        """

    @abstractmethod
    def stream(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> LLMMessage:
        """Make a streaming LLM call.

        Args:
            messages: Conversation messages in Anthropic format.
            model: Model identifier (without provider prefix).
            max_tokens: Maximum output tokens.
            system: Optional system prompt.
            tools: Optional tool definitions in Anthropic format.
            on_text_delta: Callback invoked with each text chunk.

        Returns:
            The complete LLMMessage once the stream finishes.
        """

    def format_tools(self, tool_defs: list[dict]) -> list[dict]:
        """Convert Anthropic-format tool definitions to this provider's format.

        Default implementation returns tools unchanged (Anthropic format).
        Override for providers that use a different tool format (e.g. OpenAI).
        """
        return tool_defs

    def format_messages(self, messages: list[dict]) -> list[dict]:
        """Convert Anthropic-format messages to this provider's format.

        Default implementation returns messages unchanged (Anthropic format).
        Override for providers that use a different message format (e.g. OpenAI).
        """
        return messages

    def health(self) -> bool:
        """Check if the provider is reachable and operational.

        Default implementation returns True. Providers should override this
        with a lightweight API call (e.g. list models) to verify connectivity.
        """
        return True

    def extract_env_vars(self) -> dict[str, str]:
        """Return environment variables needed for container execution.

        Used by container runners to pass provider credentials into Docker.
        Default implementation returns an empty dict.
        """
        return {}

    @staticmethod
    def parse_model_string(model: str) -> tuple[str | None, str]:
        """Parse a 'provider/model' string into (provider, model).

        If no '/' is present, returns (None, model) — the caller should
        fall back to the config's provider field.

        Examples:
            >>> LLMProvider.parse_model_string("anthropic/claude-sonnet-4-6")
            ('anthropic', 'claude-sonnet-4-6')
            >>> LLMProvider.parse_model_string("claude-sonnet-4-6")
            (None, 'claude-sonnet-4-6')
            >>> LLMProvider.parse_model_string("bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0")
            ('bedrock', 'anthropic.claude-3-5-sonnet-20241022-v2:0')
        """
        if "/" in model:
            provider, _, model_name = model.partition("/")
            return provider, model_name
        return None, model


def _resolve_provider_name(model: str, config_provider: str) -> str:
    """Determine the effective provider name from model string and config.

    If the model string contains a '/' prefix, that takes precedence.
    Otherwise, falls back to the config's provider field.
    """
    prefix, _ = LLMProvider.parse_model_string(model)
    return prefix if prefix is not None else config_provider


def _resolve_model_name(model: str) -> str:
    """Strip the provider prefix from a model string, if present."""
    _, model_name = LLMProvider.parse_model_string(model)
    return model_name


def build_provider(provider_name: str, **kwargs: Any) -> LLMProvider:
    """Instantiate an LLM provider by name.

    This is the main factory function. Lazy-imports provider modules to
    avoid pulling in optional dependencies at import time.

    Args:
        provider_name: One of "anthropic", "openai", "bedrock", "ollama".
        **kwargs: Provider-specific options (e.g. api_base, region).

    Raises:
        ValueError: If the provider name is unknown.
        ImportError: If the provider's dependencies are not installed.
    """
    name = provider_name.lower()

    if name == "anthropic":
        from creel.providers.anthropic import AnthropicProvider

        return AnthropicProvider()

    if name == "openai":
        from creel.providers.openai import OpenAIProvider

        return OpenAIProvider(api_base=kwargs.get("api_base"))

    if name == "bedrock":
        from creel.providers.bedrock import BedrockProvider

        return BedrockProvider(region=kwargs.get("region"))

    if name == "ollama":
        from creel.providers.ollama import OllamaProvider

        return OllamaProvider(api_base=kwargs.get("api_base"))

    if name == "gemini":
        from creel.providers.gemini import GeminiProvider

        return GeminiProvider()

    raise ValueError(
        f"Unknown LLM provider: {provider_name!r}. "
        f"Supported providers: anthropic, openai, bedrock, ollama, gemini"
    )
