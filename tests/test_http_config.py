"""Tests for HttpConfig model — validation, defaults, and hard upper limits."""

from __future__ import annotations

import pytest

from creel.models import ExecutorConfig, HttpConfig, ToolConfig


class TestHttpConfigDefaults:
    def test_defaults(self):
        cfg = HttpConfig()
        assert cfg.timeout == 15.0
        assert cfg.connect_timeout == 5.0
        assert cfg.max_redirects == 3
        assert cfg.max_size_mb == 5.0

    def test_custom_values(self):
        cfg = HttpConfig(timeout=30, connect_timeout=10, max_redirects=5, max_size_mb=10)
        assert cfg.timeout == 30.0
        assert cfg.connect_timeout == 10.0
        assert cfg.max_redirects == 5
        assert cfg.max_size_mb == 10.0


class TestHttpConfigHardLimits:
    def test_timeout_clamped_to_120(self):
        cfg = HttpConfig(timeout=300)
        assert cfg.timeout == 120.0

    def test_connect_timeout_clamped_to_120(self):
        cfg = HttpConfig(connect_timeout=200)
        assert cfg.connect_timeout == 120.0

    def test_timeout_at_120_allowed(self):
        cfg = HttpConfig(timeout=120)
        assert cfg.timeout == 120.0

    def test_timeout_below_120_unchanged(self):
        cfg = HttpConfig(timeout=60)
        assert cfg.timeout == 60.0


class TestHttpConfigValidation:
    def test_timeout_must_be_positive(self):
        with pytest.raises(ValueError):
            HttpConfig(timeout=0)

    def test_connect_timeout_must_be_positive(self):
        with pytest.raises(ValueError):
            HttpConfig(connect_timeout=-1)

    def test_max_redirects_non_negative(self):
        cfg = HttpConfig(max_redirects=0)
        assert cfg.max_redirects == 0

    def test_max_redirects_negative_rejected(self):
        with pytest.raises(ValueError):
            HttpConfig(max_redirects=-1)

    def test_max_size_mb_must_be_positive(self):
        with pytest.raises(ValueError):
            HttpConfig(max_size_mb=0)


class TestExecutorConfigHttp:
    def test_default_http_config(self):
        cfg = ExecutorConfig(name="fetch_url")
        assert cfg.http.timeout == 15.0
        assert cfg.http.connect_timeout == 5.0

    def test_custom_http_config(self):
        cfg = ExecutorConfig(
            name="fetch_url",
            http=HttpConfig(timeout=30, connect_timeout=10),
        )
        assert cfg.http.timeout == 30.0
        assert cfg.http.connect_timeout == 10.0


class TestToolConfigHttp:
    def test_default_http_config(self):
        cfg = ToolConfig(executor="fetch_url", description="test")
        assert cfg.http.timeout == 15.0
        assert cfg.http.connect_timeout == 5.0

    def test_custom_http_from_dict(self):
        """ToolConfig should accept http as a nested dict (from YAML)."""
        cfg = ToolConfig(
            executor="fetch_url",
            description="test",
            http={"timeout": 30, "connect_timeout": 10, "max_redirects": 5, "max_size_mb": 10},
        )
        assert cfg.http.timeout == 30.0
        assert cfg.http.connect_timeout == 10.0
        assert cfg.http.max_redirects == 5
        assert cfg.http.max_size_mb == 10.0

    def test_http_hard_limit_via_tool_config(self):
        cfg = ToolConfig(
            executor="fetch_url",
            description="test",
            http={"timeout": 999},
        )
        assert cfg.http.timeout == 120.0
