"""Tests for the ModelRouter failover chain."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creel.providers.base import (
    LLMAuthError,
    LLMMessage,
    LLMRateLimitError,
    LLMTransientError,
    TextBlock,
    Usage,
)
from creel.providers.router import ModelRouter


def _make_message(text: str = "ok") -> LLMMessage:
    return LLMMessage(
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=5, output_tokens=3),
    )


def _make_provider(healthy: bool = True, create_side_effect=None):
    """Create a mock LLMProvider."""
    p = MagicMock()
    p.health.return_value = healthy
    if create_side_effect:
        p.create.side_effect = create_side_effect
        p.stream.side_effect = create_side_effect
    else:
        p.create.return_value = _make_message()
        p.stream.return_value = _make_message()
    p.extract_env_vars.return_value = {}
    return p


class TestModelRouterCreate:
    @patch("creel.providers.router.build_provider")
    def test_primary_succeeds(self, mock_build):
        """When primary succeeds, no fallback is tried."""
        primary = _make_provider()
        mock_build.return_value = primary

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
        )
        result = router.create(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-6",
            max_tokens=100,
        )

        assert result.content[0].text == "ok"
        # build_provider called once for primary, once for fallback (chain building)
        assert mock_build.call_count >= 1

    @patch("creel.providers.router.build_provider")
    def test_failover_on_rate_limit(self, mock_build):
        """Rate limit on primary should trigger fallback."""
        primary = _make_provider(
            create_side_effect=LLMRateLimitError("rate limited", status_code=429)
        )
        fallback = _make_provider()
        fallback.create.return_value = _make_message("from fallback")

        mock_build.side_effect = [primary, fallback]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
            check_health=False,
        )
        result = router.create(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-6",
            max_tokens=100,
        )

        assert result.content[0].text == "from fallback"

    @patch("creel.providers.router.build_provider")
    def test_failover_on_transient_error(self, mock_build):
        """500/502/503 on primary should trigger fallback."""
        primary = _make_provider(
            create_side_effect=LLMTransientError("server error", status_code=500)
        )
        fallback = _make_provider()
        fallback.create.return_value = _make_message("fallback ok")

        mock_build.side_effect = [primary, fallback]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
            check_health=False,
        )
        result = router.create(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-6",
            max_tokens=100,
        )

        assert result.content[0].text == "fallback ok"

    @patch("creel.providers.router.build_provider")
    def test_auth_error_propagates_immediately(self, mock_build):
        """Auth errors should NOT trigger failover."""
        primary = _make_provider(create_side_effect=LLMAuthError("bad key", status_code=401))
        fallback = _make_provider()

        mock_build.side_effect = [primary, fallback]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
            check_health=False,
        )

        with pytest.raises(LLMAuthError, match="bad key"):
            router.create(
                messages=[{"role": "user", "content": "hi"}],
                model="claude-sonnet-4-6",
                max_tokens=100,
            )

    @patch("creel.providers.router.build_provider")
    def test_all_providers_fail(self, mock_build):
        """When all providers fail, the last error is raised."""
        primary = _make_provider(
            create_side_effect=LLMRateLimitError("rate limited", status_code=429)
        )
        fallback = _make_provider(
            create_side_effect=LLMTransientError("also down", status_code=503)
        )

        mock_build.side_effect = [primary, fallback]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
            check_health=False,
        )

        with pytest.raises(LLMTransientError, match="also down"):
            router.create(
                messages=[{"role": "user", "content": "hi"}],
                model="claude-sonnet-4-6",
                max_tokens=100,
            )

    @patch("creel.providers.router.build_provider")
    def test_unhealthy_fallback_skipped(self, mock_build):
        """Unhealthy fallback providers are skipped."""
        primary = _make_provider(
            create_side_effect=LLMRateLimitError("rate limited", status_code=429)
        )
        unhealthy = _make_provider(healthy=False)
        healthy = _make_provider()
        healthy.create.return_value = _make_message("healthy provider")

        mock_build.side_effect = [primary, unhealthy, healthy]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o", "ollama/llama3.2"],
            check_health=True,
        )
        result = router.create(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-6",
            max_tokens=100,
        )

        assert result.content[0].text == "healthy provider"

    @patch("creel.providers.router.build_provider")
    def test_no_fallback_configured(self, mock_build):
        """With no fallback, primary error propagates."""
        primary = _make_provider(
            create_side_effect=LLMRateLimitError("rate limited", status_code=429)
        )
        mock_build.return_value = primary

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=[],
        )

        with pytest.raises(LLMRateLimitError):
            router.create(
                messages=[{"role": "user", "content": "hi"}],
                model="claude-sonnet-4-6",
                max_tokens=100,
            )


class TestModelRouterStream:
    @patch("creel.providers.router.build_provider")
    def test_stream_failover(self, mock_build):
        """Streaming should also failover on transient errors."""
        primary = _make_provider(create_side_effect=LLMTransientError("down", status_code=502))
        fallback = _make_provider()
        fallback.stream.return_value = _make_message("streamed fallback")

        mock_build.side_effect = [primary, fallback]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
            check_health=False,
        )
        result = router.stream(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-6",
            max_tokens=100,
        )

        assert result.content[0].text == "streamed fallback"


class TestModelRouterHealth:
    @patch("creel.providers.router.build_provider")
    def test_health_true_when_any_healthy(self, mock_build):
        """health() returns True if at least one provider is healthy."""
        primary = _make_provider(healthy=False)
        fallback = _make_provider(healthy=True)

        mock_build.side_effect = [primary, fallback]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
        )
        assert router.health() is True

    @patch("creel.providers.router.build_provider")
    def test_health_false_when_all_unhealthy(self, mock_build):
        """health() returns False if all providers are unhealthy."""
        primary = _make_provider(healthy=False)
        fallback = _make_provider(healthy=False)

        mock_build.side_effect = [primary, fallback]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
        )
        assert router.health() is False


class TestModelRouterEnvVars:
    @patch("creel.providers.router.build_provider")
    def test_merges_env_vars_from_all_providers(self, mock_build):
        """extract_env_vars() should merge from all providers."""
        primary = _make_provider()
        primary.extract_env_vars.return_value = {"ANTHROPIC_API_KEY": "k1"}
        fallback = _make_provider()
        fallback.extract_env_vars.return_value = {"OPENAI_API_KEY": "k2"}

        mock_build.side_effect = [primary, fallback]

        router = ModelRouter(
            primary_provider="anthropic",
            primary_model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
        )
        env = router.extract_env_vars()
        assert env["ANTHROPIC_API_KEY"] == "k1"
        assert env["OPENAI_API_KEY"] == "k2"


class TestGetProviderWithFallback:
    def test_no_fallback_returns_plain_provider(self):
        from creel.providers import get_provider_with_fallback

        provider = get_provider_with_fallback(provider="anthropic", model="claude-sonnet-4-6")
        assert not isinstance(provider, ModelRouter)

    def test_with_fallback_returns_router(self):
        from creel.providers import get_provider_with_fallback

        provider = get_provider_with_fallback(
            provider="anthropic",
            model="claude-sonnet-4-6",
            fallback=["openai/gpt-4o"],
        )
        assert isinstance(provider, ModelRouter)
