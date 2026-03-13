"""Tests for guardian.network — NetworkMonitor domain filtering, size limits, and rate limiting."""

from __future__ import annotations

from typing import Any

from guardian.network import NetworkMonitor, NetworkVerdict, _domain_matches, _extract_domain
from guardian.types import NetworkPolicyConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_monitor(**overrides: Any) -> NetworkMonitor:
    """Create a NetworkMonitor with sensible test defaults."""
    defaults: dict[str, Any] = {
        "enabled": True,
        "allowed_domains": ["*.googleapis.com", "api.openai.com", "api.anthropic.com"],
        "blocked_domains": ["*.pastebin.com", "*.ngrok.io"],
        "max_request_size_mb": 10,
        "max_response_size_mb": 50,
        "rate_limit_per_minute": 100,
        "alert_on_unknown": True,
    }
    defaults.update(overrides)
    config = NetworkPolicyConfig(**defaults)
    return NetworkMonitor(config)


# ---------------------------------------------------------------------------
# _extract_domain
# ---------------------------------------------------------------------------


class TestExtractDomain:
    def test_simple_https(self):
        assert _extract_domain("https://api.openai.com/v1/chat") == "api.openai.com"

    def test_http(self):
        assert _extract_domain("http://example.com/path") == "example.com"

    def test_with_port(self):
        assert _extract_domain("https://localhost:8080/api") == "localhost"

    def test_empty_string(self):
        assert _extract_domain("") == ""

    def test_invalid_url(self):
        # urlparse is lenient — but no hostname means empty
        assert _extract_domain("not-a-url") == ""


# ---------------------------------------------------------------------------
# _domain_matches
# ---------------------------------------------------------------------------


class TestDomainMatches:
    def test_exact_match(self):
        assert _domain_matches("api.openai.com", "api.openai.com") is True

    def test_wildcard_match(self):
        assert _domain_matches("storage.googleapis.com", "*.googleapis.com") is True

    def test_wildcard_no_match(self):
        assert _domain_matches("api.openai.com", "*.googleapis.com") is False

    def test_wildcard_subdomain(self):
        assert _domain_matches("evil.pastebin.com", "*.pastebin.com") is True

    def test_no_match(self):
        assert _domain_matches("example.com", "other.com") is False


# ---------------------------------------------------------------------------
# NetworkMonitor.check_domain
# ---------------------------------------------------------------------------


class TestCheckDomain:
    def test_allowed_domain(self):
        m = _make_monitor()
        v = m.check_domain("https://api.openai.com/v1/chat")
        assert v.allowed is True
        assert v.domain == "api.openai.com"

    def test_allowed_wildcard(self):
        m = _make_monitor()
        v = m.check_domain("https://storage.googleapis.com/bucket")
        assert v.allowed is True

    def test_blocked_domain(self):
        m = _make_monitor()
        v = m.check_domain("https://evil.pastebin.com/raw/abc")
        assert v.allowed is False
        assert "blocked" in v.reason

    def test_blocked_takes_precedence(self):
        """If a domain matches both allowed and blocked, blocked wins."""
        m = _make_monitor(
            allowed_domains=["*.example.com"],
            blocked_domains=["*.example.com"],
        )
        v = m.check_domain("https://api.example.com/v1")
        assert v.allowed is False

    def test_unknown_domain_denied_with_allowlist(self):
        m = _make_monitor()
        v = m.check_domain("https://unknown.example.com/data")
        assert v.allowed is False
        assert v.is_unknown_domain is True
        assert "not in allowed" in v.reason

    def test_permissive_when_no_allowlist(self):
        m = _make_monitor(allowed_domains=[])
        v = m.check_domain("https://anything.example.com/path")
        assert v.allowed is True

    def test_invalid_url(self):
        m = _make_monitor()
        v = m.check_domain("")
        assert v.allowed is False
        assert "invalid" in v.reason


# ---------------------------------------------------------------------------
# NetworkMonitor.check_request_size
# ---------------------------------------------------------------------------


class TestCheckRequestSize:
    def test_within_limit(self):
        m = _make_monitor(max_request_size_mb=10)
        v = m.check_request_size(1024)
        assert v.allowed is True

    def test_exceeds_limit(self):
        m = _make_monitor(max_request_size_mb=1)
        # 2 MB in bytes
        v = m.check_request_size(2 * 1024 * 1024)
        assert v.allowed is False
        assert "exceeds" in v.reason

    def test_at_exact_limit(self):
        m = _make_monitor(max_request_size_mb=1)
        v = m.check_request_size(1 * 1024 * 1024)
        assert v.allowed is True


# ---------------------------------------------------------------------------
# NetworkMonitor.check_response_size
# ---------------------------------------------------------------------------


class TestCheckResponseSize:
    def test_within_limit(self):
        m = _make_monitor(max_response_size_mb=50)
        v = m.check_response_size(10 * 1024 * 1024)
        assert v.allowed is True

    def test_exceeds_limit(self):
        m = _make_monitor(max_response_size_mb=1)
        v = m.check_response_size(2 * 1024 * 1024)
        assert v.allowed is False
        assert "exceeds" in v.reason


# ---------------------------------------------------------------------------
# NetworkMonitor.check_rate_limit
# ---------------------------------------------------------------------------


class TestCheckRateLimit:
    def test_within_limit(self):
        m = _make_monitor(rate_limit_per_minute=10)
        for _ in range(10):
            v = m.check_rate_limit("test_executor")
            assert v.allowed is True

    def test_exceeds_limit(self):
        m = _make_monitor(rate_limit_per_minute=5)
        for _ in range(5):
            v = m.check_rate_limit("test_executor")
            assert v.allowed is True
        # 6th request should be denied
        v = m.check_rate_limit("test_executor")
        assert v.allowed is False
        assert "rate limit" in v.reason

    def test_separate_executors(self):
        m = _make_monitor(rate_limit_per_minute=2)
        m.check_rate_limit("exec_a")
        m.check_rate_limit("exec_a")
        # exec_a is at limit
        v = m.check_rate_limit("exec_a")
        assert v.allowed is False
        # exec_b should still be fine
        v = m.check_rate_limit("exec_b")
        assert v.allowed is True


# ---------------------------------------------------------------------------
# NetworkMonitor.check_request (combined)
# ---------------------------------------------------------------------------


class TestCheckRequest:
    def test_allowed_request(self):
        m = _make_monitor()
        v = m.check_request(
            "https://api.openai.com/v1/chat",
            executor="test",
            method="POST",
            request_size_bytes=1024,
        )
        assert v.allowed is True
        assert v.domain == "api.openai.com"

    def test_blocked_domain_request(self):
        m = _make_monitor()
        v = m.check_request(
            "https://evil.pastebin.com/raw/abc",
            executor="test",
        )
        assert v.allowed is False
        # Should be logged
        assert len(m.request_log) == 1
        assert m.request_log[0]["blocked"] is True

    def test_oversized_request(self):
        m = _make_monitor(max_request_size_mb=0.001)  # ~1 KB
        v = m.check_request(
            "https://api.openai.com/v1/chat",
            executor="test",
            request_size_bytes=2048,
        )
        assert v.allowed is False
        assert "exceeds" in v.reason

    def test_rate_limited_request(self):
        m = _make_monitor(rate_limit_per_minute=1)
        m.check_request("https://api.openai.com/v1/chat", executor="test")
        v = m.check_request("https://api.openai.com/v1/chat", executor="test")
        assert v.allowed is False
        assert "rate limit" in v.reason


# ---------------------------------------------------------------------------
# NetworkMonitor.record_response
# ---------------------------------------------------------------------------


class TestRecordResponse:
    def test_normal_response(self):
        m = _make_monitor()
        alert = m.record_response(
            "https://api.openai.com/v1/chat",
            executor="test",
            response_size_bytes=1024,
            status_code=200,
        )
        assert alert is None
        assert len(m.request_log) == 1
        assert m.request_log[0]["status_code"] == 200

    def test_oversized_response_alert(self):
        m = _make_monitor(max_response_size_mb=0.001)
        alert = m.record_response(
            "https://api.openai.com/v1/chat",
            executor="test",
            response_size_bytes=2048,
        )
        assert alert is not None
        assert alert.allowed is False
        assert "exceeds" in alert.reason


# ---------------------------------------------------------------------------
# NetworkPolicyConfig defaults
# ---------------------------------------------------------------------------


class TestNetworkPolicyConfig:
    def test_defaults(self):
        config = NetworkPolicyConfig()
        assert config.enabled is False
        assert config.allowed_domains == []
        assert config.blocked_domains == []
        assert config.max_request_size_mb == 10.0
        assert config.max_response_size_mb == 50.0
        assert config.rate_limit_per_minute == 100
        assert config.alert_on_unknown is True

    def test_custom_config(self):
        config = NetworkPolicyConfig(
            enabled=True,
            allowed_domains=["*.example.com"],
            blocked_domains=["evil.com"],
            max_request_size_mb=5,
            rate_limit_per_minute=50,
        )
        assert config.enabled is True
        assert len(config.allowed_domains) == 1
        assert config.max_request_size_mb == 5


# ---------------------------------------------------------------------------
# NetworkVerdict dataclass
# ---------------------------------------------------------------------------


class TestNetworkVerdict:
    def test_default(self):
        v = NetworkVerdict(allowed=True)
        assert v.allowed is True
        assert v.reason == ""
        assert v.domain == ""
        assert v.is_unknown_domain is False


# ---------------------------------------------------------------------------
# Integration: GuardianConfig with network_policy
# ---------------------------------------------------------------------------


class TestGuardianConfigIntegration:
    def test_default_network_policy(self):
        from guardian.types import GuardianConfig

        config = GuardianConfig()
        assert config.network_policy.enabled is False

    def test_enabled_network_policy(self):
        from guardian.types import GuardianConfig

        config = GuardianConfig(
            network_policy={
                "enabled": True,
                "allowed_domains": ["*.googleapis.com"],
                "blocked_domains": ["*.ngrok.io"],
            }
        )
        assert config.network_policy.enabled is True
        assert "*.googleapis.com" in config.network_policy.allowed_domains
