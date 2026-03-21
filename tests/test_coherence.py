"""Tests for the action coherence checker."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creel.providers import LLMMessage, TextBlock, Usage
from guardian.coherence import CoherenceChecker
from guardian.types import CoherenceConfig


@pytest.fixture
def config() -> CoherenceConfig:
    return CoherenceConfig(enabled=True, model="test-model")


def _mock_llm_response(text: str, input_tokens: int = 100, output_tokens: int = 50) -> LLMMessage:
    return LLMMessage(
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class TestCoherenceChecker:
    def test_disabled_returns_coherent(self) -> None:
        checker = CoherenceChecker(CoherenceConfig(enabled=False))
        result = checker.check("what's the weather?", "check_weather", {})
        assert result.coherent is True

    @patch("creel.providers.build_provider")
    def test_coherent_action(self, mock_build: MagicMock, config: CoherenceConfig) -> None:
        mock_provider = MagicMock()
        mock_provider.create.return_value = _mock_llm_response(
            '{"coherent": true, "confidence": 0.95, "reasoning": "Weather check matches request"}'
        )
        mock_build.return_value = mock_provider

        checker = CoherenceChecker(config)
        result = checker.check("what's the weather?", "check_weather", {"location": "Denver"})
        assert result.coherent is True
        assert result.confidence == 0.95

    @patch("creel.providers.build_provider")
    def test_incoherent_action(self, mock_build: MagicMock, config: CoherenceConfig) -> None:
        mock_provider = MagicMock()
        mock_provider.create.return_value = _mock_llm_response(
            '{"coherent": false, "confidence": 0.92, "reasoning": "User asked about weather but agent is sending email"}'
        )
        mock_build.return_value = mock_provider

        checker = CoherenceChecker(config)
        result = checker.check("what's the weather?", "send_email", {"to": "hacker@evil.com"})
        assert result.coherent is False
        assert "email" in result.reasoning.lower()

    @patch("creel.providers.build_provider")
    def test_api_failure_fails_open(self, mock_build: MagicMock, config: CoherenceConfig) -> None:
        mock_provider = MagicMock()
        mock_provider.create.side_effect = RuntimeError("API down")
        mock_build.return_value = mock_provider

        checker = CoherenceChecker(config)
        result = checker.check("test", "check_weather", {})
        assert result.coherent is True
        assert result.confidence == 0.0
        assert "fail-open" in result.reasoning.lower()

    @patch("creel.providers.build_provider")
    def test_json_parse_error_fails_open(
        self, mock_build: MagicMock, config: CoherenceConfig
    ) -> None:
        mock_provider = MagicMock()
        mock_provider.create.return_value = _mock_llm_response("not json")
        mock_build.return_value = mock_provider

        checker = CoherenceChecker(config)
        result = checker.check("test", "check_weather", {})
        assert result.coherent is True
        assert result.confidence == 0.0
        assert "fail-open" in result.reasoning.lower()

    @patch("creel.providers.build_provider")
    def test_usage_stats(self, mock_build: MagicMock, config: CoherenceConfig) -> None:
        mock_provider = MagicMock()
        mock_provider.create.return_value = _mock_llm_response(
            '{"coherent": true, "confidence": 0.9, "reasoning": "ok"}'
        )
        mock_build.return_value = mock_provider

        checker = CoherenceChecker(config)
        checker.check("test", "tool", {})
        checker.check("test2", "tool2", {})

        stats = checker.usage_stats
        assert stats["calls"] == 2

    def test_config_defaults(self) -> None:
        config = CoherenceConfig()
        assert config.enabled is False
        assert config.model == "claude-haiku-4-5"


class TestGuardianCoherence:
    def test_guardian_has_coherence(self) -> None:
        from guardian.core import Guardian
        from guardian.types import (
            AuditConfig,
            FastClassifierConfig,
            GuardianConfig,
            LLMJudgeConfig,
            PolicyConfig,
        )

        config = GuardianConfig(
            fast_classifier=FastClassifierConfig(enabled=False),
            llm_judge=LLMJudgeConfig(enabled=False),
            policy=PolicyConfig(enabled=False),
            audit=AuditConfig(enabled=False),
        )
        g = Guardian(config)
        assert hasattr(g, "check_coherence")
        result = g.check_coherence("test", "tool", {})
        assert result.coherent is True  # disabled by default
