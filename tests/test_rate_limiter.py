"""Tests for the LLM rate limiter."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from creel.rate_limiter import (
    RateLimiter,
    RateLimitExceeded,
    UsageSnapshot,
    configure_rate_limiter,
    estimate_cost,
    get_rate_limiter,
    reset_global_limiter,
)


class TestEstimateCost:
    def test_known_model(self):
        cost = estimate_cost("claude-sonnet-4-20250514", 1000, 500)
        # input: 1000 * 3.00 / 1M = 0.003
        # output: 500 * 15.00 / 1M = 0.0075
        assert abs(cost - 0.0105) < 1e-6

    def test_unknown_model_uses_default(self):
        cost = estimate_cost("some-future-model", 1000, 500)
        # Default pricing same as sonnet
        assert abs(cost - 0.0105) < 1e-6

    def test_zero_tokens(self):
        assert estimate_cost("claude-sonnet-4-20250514", 0, 0) == 0.0

    def test_haiku_cheaper(self):
        haiku_cost = estimate_cost("claude-haiku-4-5-20251001", 1000, 1000)
        sonnet_cost = estimate_cost("claude-sonnet-4-20250514", 1000, 1000)
        assert haiku_cost < sonnet_cost


class TestRateLimiterBasic:
    def test_create_with_defaults(self):
        rl = RateLimiter()
        assert rl.requests_per_minute == 30
        assert rl.requests_per_hour == 500
        assert rl.tokens_per_day == 1_000_000
        assert rl.cost_per_day_usd == 10.00

    def test_check_passes_when_under_limit(self):
        rl = RateLimiter(requests_per_minute=10)
        rl.check(block=False)  # Should not raise

    def test_record_tracks_usage(self):
        rl = RateLimiter()
        rl.record("claude-sonnet-4-20250514", input_tokens=100, output_tokens=50)
        usage = rl.get_usage()
        assert usage.requests_last_minute == 1
        assert usage.tokens_today == 150
        assert usage.cost_today_usd > 0

    def test_get_usage_returns_snapshot(self):
        rl = RateLimiter(
            requests_per_minute=30,
            requests_per_hour=500,
            tokens_per_day=1_000_000,
            cost_per_day_usd=10.00,
        )
        usage = rl.get_usage()
        assert isinstance(usage, UsageSnapshot)
        assert usage.requests_per_minute_limit == 30
        assert usage.requests_per_hour_limit == 500
        assert usage.tokens_per_day_limit == 1_000_000
        assert usage.cost_per_day_limit_usd == 10.00
        assert not usage.override_active


class TestTokenBucket:
    def test_bucket_exhaustion_raises(self):
        rl = RateLimiter(requests_per_minute=2, queue_timeout=0.0)
        rl.check(block=False)
        rl.check(block=False)
        with pytest.raises(RateLimitExceeded, match="requests_per_minute"):
            rl.check(block=False)

    def test_bucket_refills_over_time(self):
        rl = RateLimiter(requests_per_minute=60, queue_timeout=0.0)
        # 60 RPM = 1 per second, bucket starts full at 60
        for _ in range(60):
            rl.check(block=False)
        # Bucket exhausted
        with pytest.raises(RateLimitExceeded):
            rl.check(block=False)

    def test_blocking_check_waits_for_token(self):
        rl = RateLimiter(requests_per_minute=60, queue_timeout=2.0)
        # Drain to 0
        for _ in range(60):
            rl.check(block=False)
        # Blocking check should succeed after waiting (refill rate = 1/sec)
        start = time.monotonic()
        rl.check(block=True)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0  # Should succeed within timeout


class TestHourlyLimit:
    def test_hourly_limit_exceeded(self):
        rl = RateLimiter(
            requests_per_minute=1000,
            requests_per_hour=3,
            queue_timeout=0.0,
        )
        # Record 3 requests to fill the hourly window
        for _ in range(3):
            rl.record("claude-sonnet-4-20250514", 10, 10)

        with pytest.raises(RateLimitExceeded, match="requests_per_hour"):
            rl.check(block=False)


class TestDailyLimits:
    def test_token_daily_limit(self):
        rl = RateLimiter(
            requests_per_minute=1000,
            requests_per_hour=10000,
            tokens_per_day=100,
            queue_timeout=0.0,
        )
        # Record 60 + 60 = 120 tokens, exceeding 100 limit
        rl.record("claude-sonnet-4-20250514", 60, 60)
        with pytest.raises(RateLimitExceeded, match="tokens_per_day"):
            rl.check(block=False)

    def test_cost_daily_limit(self):
        rl = RateLimiter(
            requests_per_minute=1000,
            requests_per_hour=10000,
            tokens_per_day=100_000_000,
            cost_per_day_usd=0.001,  # Very low limit
            queue_timeout=0.0,
        )
        # Record a call that exceeds the cost cap
        rl.record("claude-sonnet-4-20250514", 10000, 10000)
        with pytest.raises(RateLimitExceeded, match="cost_per_day_usd"):
            rl.check(block=False)


class TestOverride:
    def test_override_bypasses_limits(self):
        rl = RateLimiter(requests_per_minute=1, queue_timeout=0.0)
        rl.check(block=False)  # Use up the one allowed
        # Without override, next should fail
        with pytest.raises(RateLimitExceeded):
            rl.check(block=False)
        # Activate override
        rl.override(duration_seconds=60)
        rl.check(block=False)  # Should pass now

    def test_override_expires(self):
        rl = RateLimiter(requests_per_minute=1, queue_timeout=0.0)
        rl.check(block=False)
        rl.override(duration_seconds=0.01)  # 10ms
        time.sleep(0.02)  # Wait for expiry
        with pytest.raises(RateLimitExceeded):
            rl.check(block=False)

    def test_usage_shows_override_active(self):
        rl = RateLimiter()
        rl.override(60)
        usage = rl.get_usage()
        assert usage.override_active
        assert usage.override_expires_at is not None


class TestAlerts:
    def test_alert_fires_at_80_percent(self):
        alerts: list[tuple] = []

        def on_alert(level, limit_type, current, limit):
            alerts.append((level, limit_type, current, limit))

        rl = RateLimiter(
            requests_per_minute=1000,
            requests_per_hour=10000,
            tokens_per_day=100,
            cost_per_day_usd=100.0,
            on_alert=on_alert,
        )
        # Record 85 tokens out of 100 limit = 85%
        rl.record("claude-sonnet-4-20250514", 45, 40)
        assert len(alerts) == 1
        assert alerts[0][0] == "approaching_limit"
        assert alerts[0][1] == "tokens_per_day"

    def test_alert_fires_at_100_percent(self):
        alerts: list[tuple] = []

        def on_alert(level, limit_type, current, limit):
            alerts.append((level, limit_type, current, limit))

        rl = RateLimiter(
            requests_per_minute=1000,
            requests_per_hour=10000,
            tokens_per_day=100,
            cost_per_day_usd=100.0,
            on_alert=on_alert,
        )
        rl.record("claude-sonnet-4-20250514", 55, 50)
        alert_types = [a[0] for a in alerts]
        assert "limit_hit" in alert_types

    def test_alert_not_repeated(self):
        alerts: list[tuple] = []

        def on_alert(level, limit_type, current, limit):
            alerts.append((level, limit_type, current, limit))

        rl = RateLimiter(
            requests_per_minute=1000,
            requests_per_hour=10000,
            tokens_per_day=1000,
            cost_per_day_usd=100.0,
            on_alert=on_alert,
        )
        # Two calls that both keep us above 80%
        rl.record("claude-sonnet-4-20250514", 450, 400)
        count_after_first = len(alerts)
        rl.record("claude-sonnet-4-20250514", 10, 10)
        # Should not fire again for same threshold
        assert len(alerts) == count_after_first


class TestUsageHistory:
    def test_in_memory_history(self):
        rl = RateLimiter()
        rl.record("claude-sonnet-4-20250514", 100, 50)
        history = rl.get_usage_history(days=1)
        assert len(history) == 1
        assert history[0]["requests"] == 1
        assert history[0]["total_tokens"] == 150

    def test_persisted_history(self, tmp_path):
        usage_dir = tmp_path / "usage"
        rl = RateLimiter(usage_dir=usage_dir)
        rl.record("claude-sonnet-4-20250514", 200, 100)

        history = rl.get_usage_history(days=1)
        assert len(history) == 1
        assert history[0]["requests"] == 1
        assert history[0]["input_tokens"] == 200
        assert history[0]["output_tokens"] == 100

    def test_persisted_file_created(self, tmp_path):
        import datetime

        usage_dir = tmp_path / "usage"
        rl = RateLimiter(usage_dir=usage_dir)
        rl.record("claude-sonnet-4-20250514", 100, 50)

        today = datetime.datetime.now(datetime.UTC).date().strftime("%Y-%m-%d")
        fpath = usage_dir / f"{today}.jsonl"
        assert fpath.exists()
        data = json.loads(fpath.read_text().strip())
        assert data["input_tokens"] == 100
        assert data["output_tokens"] == 50

    def test_history_multiple_days(self):
        rl = RateLimiter()
        history = rl.get_usage_history(days=7)
        assert len(history) == 7
        # All days should have zero usage
        assert all(d["requests"] == 0 for d in history)


class TestGlobalSingleton:
    def setup_method(self):
        reset_global_limiter()

    def teardown_method(self):
        reset_global_limiter()

    def test_no_limiter_by_default(self):
        assert get_rate_limiter() is None

    def test_configure_sets_global(self):
        limiter = configure_rate_limiter(
            requests_per_minute=10,
            requests_per_hour=100,
        )
        assert get_rate_limiter() is limiter
        assert limiter.requests_per_minute == 10

    def test_reset_clears_global(self):
        configure_rate_limiter()
        assert get_rate_limiter() is not None
        reset_global_limiter()
        assert get_rate_limiter() is None


class TestRateLimitExceeded:
    def test_exception_attributes(self):
        exc = RateLimitExceeded(
            limit_type="requests_per_minute",
            current=30,
            limit=30,
            retry_after=2.0,
        )
        assert exc.limit_type == "requests_per_minute"
        assert exc.current == 30
        assert exc.limit == 30
        assert exc.retry_after == 2.0
        assert "requests_per_minute" in str(exc)


class TestRateLimitConfig:
    def test_config_model_defaults(self):
        from creel.models import RateLimitConfig

        cfg = RateLimitConfig()
        assert cfg.requests_per_minute == 30
        assert cfg.requests_per_hour == 500
        assert cfg.tokens_per_day == 1_000_000
        assert cfg.cost_per_day_usd == 10.00
        assert cfg.enabled is True

    def test_config_in_llm_config(self):
        from creel.models import LLMConfig

        llm = LLMConfig()
        assert llm.rate_limits is not None
        assert llm.rate_limits.requests_per_minute == 30

    def test_config_from_yaml_dict(self):
        from creel.models import LLMConfig

        llm = LLMConfig(
            **{
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "rate_limits": {
                    "requests_per_minute": 60,
                    "requests_per_hour": 1000,
                    "tokens_per_day": 2000000,
                    "cost_per_day_usd": 20.0,
                },
            }
        )
        assert llm.rate_limits.requests_per_minute == 60
        assert llm.rate_limits.cost_per_day_usd == 20.0


class TestLLMIntegration:
    """Test that rate limiter is checked during LLM calls."""

    def test_call_llm_checks_rate_limit(self, monkeypatch):
        """Verify call_llm checks the rate limiter before making API call."""
        from creel import llm as llm_mod
        from creel.rate_limiter import configure_rate_limiter, reset_global_limiter

        reset_global_limiter()
        limiter = configure_rate_limiter(
            requests_per_minute=1,
            queue_timeout=0.0,
        )

        # Drain the bucket
        limiter.check(block=False)

        # Mock the client so we don't need actual API credentials
        mock_client = MagicMock()
        monkeypatch.setattr(llm_mod, "_get_client", lambda: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        from creel.models import LLMConfig

        config = LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100)

        with pytest.raises(RateLimitExceeded, match="requests_per_minute"):
            llm_mod.call_llm(
                messages=[{"role": "user", "content": "hello"}],
                config=config,
            )

        reset_global_limiter()

    def test_call_llm_records_usage(self, monkeypatch):
        """Verify call_llm records usage after a successful call."""
        from creel import llm as llm_mod
        from creel.rate_limiter import configure_rate_limiter, reset_global_limiter

        reset_global_limiter()
        limiter = configure_rate_limiter(requests_per_minute=100)

        # Create a mock response with usage
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 50
        mock_response.usage.output_tokens = 25
        mock_response.content = []

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        monkeypatch.setattr(llm_mod, "_get_client", lambda: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        from creel.models import LLMConfig

        config = LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100)
        llm_mod.call_llm(
            messages=[{"role": "user", "content": "hello"}],
            config=config,
        )

        usage = limiter.get_usage()
        assert usage.requests_last_minute == 1
        assert usage.tokens_today == 75

        reset_global_limiter()

    def test_no_limiter_does_not_block(self, monkeypatch):
        """Without a configured limiter, calls proceed normally."""
        from creel import llm as llm_mod
        from creel.rate_limiter import reset_global_limiter

        reset_global_limiter()

        mock_response = MagicMock()
        mock_response.usage.input_tokens = 50
        mock_response.usage.output_tokens = 25
        mock_response.content = []

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        monkeypatch.setattr(llm_mod, "_get_client", lambda: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        from creel.models import LLMConfig

        config = LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100)
        result = llm_mod.call_llm(
            messages=[{"role": "user", "content": "hello"}],
            config=config,
        )
        assert result is mock_response

        reset_global_limiter()
