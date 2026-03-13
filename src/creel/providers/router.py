"""ModelRouter — failover chain that wraps build_provider with automatic fallback.

When the primary provider fails with a retryable error (429/500/502/503), the
router automatically tries the next provider/model in the configured fallback
chain.  Auth errors and non-retryable errors propagate immediately.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable

from creel.providers.base import (
    LLMAuthError,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMTransientError,
    _resolve_model_name,
    _resolve_provider_name,
    build_provider,
)

logger = logging.getLogger(__name__)

# Error types that trigger failover to the next provider
_FAILOVER_ERRORS = (LLMRateLimitError, LLMTransientError)


class ModelRouter(LLMProvider):
    """Routes LLM calls through a failover chain of providers.

    The router tries the primary provider first.  If it fails with a retryable
    error, it walks through the ``fallback`` list in order, skipping providers
    that are unhealthy (when ``check_health`` is True).

    Args:
        primary_provider: Default provider name from config.
        primary_model: Default model string (may include "provider/" prefix).
        fallback: List of "provider/model" strings to try on failure.
        api_base: Optional custom endpoint forwarded to providers.
        region: Optional AWS region forwarded to Bedrock.
        check_health: If True, call ``health()`` before each fallback attempt
            and skip unhealthy providers.
    """

    def __init__(
        self,
        primary_provider: str = "anthropic",
        primary_model: str = "claude-sonnet-4-20250514",
        fallback: list[str] | None = None,
        api_base: str | None = None,
        region: str | None = None,
        check_health: bool = True,
    ) -> None:
        self._primary_provider = primary_provider
        self._primary_model = primary_model
        self._fallback = fallback or []
        self._api_base = api_base
        self._region = region
        self._check_health = check_health

    @functools.cached_property
    def _chain(self) -> list[tuple[LLMProvider, str]]:
        """Build and cache the ordered list of (provider_instance, model_name) to try.

        Returns the primary first, then each fallback entry.
        """
        chain: list[tuple[LLMProvider, str]] = []

        # Primary
        effective_provider = _resolve_provider_name(self._primary_model, self._primary_provider)
        model_name = _resolve_model_name(self._primary_model)
        provider = build_provider(effective_provider, api_base=self._api_base, region=self._region)
        chain.append((provider, model_name))

        # Fallbacks
        for fallback_spec in self._fallback:
            fb_provider_name, fb_model = LLMProvider.parse_model_string(fallback_spec)
            if fb_provider_name is None:
                fb_provider_name = self._primary_provider
            fb_instance = build_provider(
                fb_provider_name, api_base=self._api_base, region=self._region
            )
            chain.append((fb_instance, fb_model))

        return chain

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
        chain = self._chain
        last_exc: LLMProviderError | None = None

        for i, (provider, provider_model) in enumerate(chain):
            label = f"{provider.__class__.__name__}/{provider_model}"

            # Health check for fallback entries (skip index 0 = primary)
            if i > 0 and self._check_health:
                try:
                    if not provider.health():
                        logger.info("Skipping unhealthy provider %s", label)
                        continue
                except Exception:
                    logger.info("Health check failed for %s, skipping", label)
                    continue

            try:
                logger.debug("Trying provider %s", label)
                return provider.create(
                    messages=messages,
                    model=provider_model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    timeout=timeout,
                )
            except _FAILOVER_ERRORS as exc:
                last_exc = exc
                logger.warning(
                    "Provider %s failed with %s (status %s), trying next in chain",
                    label,
                    type(exc).__name__,
                    exc.status_code,
                )
                continue
            except LLMAuthError:
                # Auth errors should not trigger failover — propagate immediately
                raise

        # All providers exhausted
        if last_exc is not None:
            raise last_exc
        raise LLMProviderError("No providers available in failover chain")

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
        chain = self._chain
        last_exc: LLMProviderError | None = None

        # Track whether any text deltas have been emitted.  Once the user has
        # received partial output we cannot transparently retry on another
        # provider — doing so would produce garbled/duplicated text.
        delta_emitted = False

        def _guarded_delta(text: str) -> None:
            nonlocal delta_emitted
            delta_emitted = True
            if on_text_delta:
                on_text_delta(text)

        for i, (provider, provider_model) in enumerate(chain):
            label = f"{provider.__class__.__name__}/{provider_model}"

            if i > 0 and self._check_health:
                try:
                    if not provider.health():
                        logger.info("Skipping unhealthy provider %s", label)
                        continue
                except Exception:
                    logger.info("Health check failed for %s, skipping", label)
                    continue

            try:
                logger.debug("Trying streaming provider %s", label)
                return provider.stream(
                    messages=messages,
                    model=provider_model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    on_text_delta=_guarded_delta if on_text_delta else None,
                )
            except _FAILOVER_ERRORS as exc:
                if delta_emitted:
                    # Partial output already sent to the caller — cannot retry
                    # transparently without garbling.  Propagate the error.
                    logger.warning(
                        "Streaming provider %s failed after partial output, cannot failover",
                        label,
                    )
                    raise
                last_exc = exc
                logger.warning(
                    "Streaming provider %s failed with %s (status %s), trying next",
                    label,
                    type(exc).__name__,
                    exc.status_code,
                )
                continue
            except LLMAuthError:
                raise

        if last_exc is not None:
            raise last_exc
        raise LLMProviderError("No providers available in failover chain")

    def health(self) -> bool:
        """Returns True if at least one provider in the chain is healthy."""
        chain = self._chain
        for provider, _ in chain:
            try:
                if provider.health():
                    return True
            except Exception:
                continue
        return False

    def extract_env_vars(self) -> dict[str, str]:
        """Merge env vars from all providers in the chain."""
        env: dict[str, str] = {}
        chain = self._chain
        for provider, _ in chain:
            env.update(provider.extract_env_vars())
        return env
