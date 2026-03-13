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
from creel.providers.router import ModelRouter

__all__ = [
    "ContentBlock",
    "LLMAuthError",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMTransientError",
    "ModelRouter",
    "TextBlock",
    "ToolUseBlock",
    "Usage",
    "_resolve_model_name",
    "_resolve_provider_name",
    "build_provider",
    "get_provider",
    "get_provider_with_fallback",
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


def get_provider_with_fallback(
    provider: str = "anthropic",
    model: str = "",
    fallback: list[str] | None = None,
    api_base: str | None = None,
    region: str | None = None,
) -> LLMProvider:
    """Factory that returns a ModelRouter when fallback is configured.

    If ``fallback`` is empty or None, returns a plain provider (no router
    overhead).  Otherwise wraps the primary + fallback chain in a ModelRouter.
    """
    if not fallback:
        return get_provider(provider=provider, model=model, api_base=api_base, region=region)

    return ModelRouter(
        primary_provider=provider,
        primary_model=model,
        fallback=fallback,
        api_base=api_base,
        region=region,
    )
