"""LLM provider abstraction — factory and re-exports."""

from creel.providers.base import (
    ContentBlock,
    LLMAuthError,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMTransientError,
    TextBlock,
    ToolUseBlock,
    Usage,
    _resolve_model_name,
    _resolve_provider_name,
    build_provider,
)

__all__ = [
    "ContentBlock",
    "LLMAuthError",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMTransientError",
    "TextBlock",
    "ToolUseBlock",
    "Usage",
    "_resolve_model_name",
    "_resolve_provider_name",
    "build_provider",
    "get_provider",
]


def get_provider(
    provider: str = "anthropic",
    model: str = "",
    api_base: str | None = None,
    region: str | None = None,
) -> LLMProvider:
    """Convenience factory that resolves provider from model string and config.

    Args:
        provider: Default provider name from config.
        model: Model string, possibly with 'provider/model' prefix.
        api_base: Optional custom API endpoint.
        region: Optional AWS region (for Bedrock).

    Returns:
        An instantiated LLMProvider.
    """
    effective_provider = _resolve_provider_name(model, provider)
    return build_provider(effective_provider, api_base=api_base, region=region)
