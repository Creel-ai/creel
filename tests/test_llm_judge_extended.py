"""Extended tests for the LLM judge — edge cases, parsing, and should_run logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from guardian.llm_judge import LLMJudge
from guardian.types import LLMJudgeConfig


@pytest.fixture
def config() -> LLMJudgeConfig:
    return LLMJudgeConfig(enabled=True, model="test-model", max_tokens=256, timeout=3.0)


@pytest.fixture
def uncertain_config() -> LLMJudgeConfig:
    return LLMJudgeConfig(
        enabled=True,
        model="test-model",
        uncertain_only=True,
        uncertain_low=0.5,
        uncertain_high=0.85,
    )


def _mock_response(text: str, input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


class TestShouldRun:
    def test_disabled_never_runs(self) -> None:
        judge = LLMJudge(LLMJudgeConfig(enabled=False))
        assert judge.should_run(0.5) is False
        assert judge.should_run(None) is False

    def test_enabled_no_uncertain_always_runs(self, config: LLMJudgeConfig) -> None:
        config.uncertain_only = False
        judge = LLMJudge(config)
        assert judge.should_run(0.1) is True
        assert judge.should_run(0.9) is True
        assert judge.should_run(None) is True

    def test_uncertain_only_in_range(self, uncertain_config: LLMJudgeConfig) -> None:
        judge = LLMJudge(uncertain_config)
        assert judge.should_run(0.5) is True
        assert judge.should_run(0.7) is True
        assert judge.should_run(0.85) is True

    def test_uncertain_only_below_range(self, uncertain_config: LLMJudgeConfig) -> None:
        judge = LLMJudge(uncertain_config)
        assert judge.should_run(0.3) is False
        assert judge.should_run(0.0) is False

    def test_uncertain_only_above_range(self, uncertain_config: LLMJudgeConfig) -> None:
        judge = LLMJudge(uncertain_config)
        assert judge.should_run(0.9) is False
        assert judge.should_run(1.0) is False

    def test_uncertain_only_none_confidence_runs(self, uncertain_config: LLMJudgeConfig) -> None:
        judge = LLMJudge(uncertain_config)
        assert judge.should_run(None) is True


class TestJudgeParsing:
    @patch("creel.llm._get_client")
    def test_json_with_extra_whitespace(
        self, mock_get_client: MagicMock, config: LLMJudgeConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '  \n{"is_injection": false, "confidence": 0.2, "reasoning": "Normal"}\n  '
        )
        mock_get_client.return_value = mock_client
        judge = LLMJudge(config)
        result = judge.judge("hello")
        assert result is not None
        assert result.is_injection is False

    @patch("creel.llm._get_client")
    def test_missing_reasoning_field(
        self, mock_get_client: MagicMock, config: LLMJudgeConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"is_injection": true, "confidence": 0.9}'
        )
        mock_get_client.return_value = mock_client
        judge = LLMJudge(config)
        result = judge.judge("test")
        assert result is not None
        assert result.is_injection is True
        assert result.reasoning == ""

    @patch("creel.llm._get_client")
    def test_missing_confidence_defaults_zero(
        self, mock_get_client: MagicMock, config: LLMJudgeConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response('{"is_injection": false}')
        mock_get_client.return_value = mock_client
        judge = LLMJudge(config)
        result = judge.judge("test")
        assert result is not None
        assert result.confidence == 0.0

    @patch("creel.llm._get_client")
    def test_missing_is_injection_defaults_false(
        self, mock_get_client: MagicMock, config: LLMJudgeConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"confidence": 0.5, "reasoning": "unclear"}'
        )
        mock_get_client.return_value = mock_client
        judge = LLMJudge(config)
        result = judge.judge("test")
        assert result is not None
        assert result.is_injection is False

    @patch("creel.llm._get_client")
    def test_empty_response_falls_through(
        self, mock_get_client: MagicMock, config: LLMJudgeConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response("")
        mock_get_client.return_value = mock_client
        judge = LLMJudge(config)
        result = judge.judge("test")
        assert result is not None
        assert result.is_injection is False
        assert "failed" in result.reasoning.lower()

    @patch("creel.llm._get_client")
    def test_json_wrapped_in_markdown(
        self, mock_get_client: MagicMock, config: LLMJudgeConfig
    ) -> None:
        """If the LLM wraps JSON in ```json blocks, parsing should fail gracefully."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '```json\n{"is_injection": true, "confidence": 0.95}\n```'
        )
        mock_get_client.return_value = mock_client
        judge = LLMJudge(config)
        result = judge.judge("test")
        # This will fail JSON parsing and fall through
        assert result is not None
        assert result.is_injection is False

    @patch("creel.llm._get_client")
    def test_timeout_falls_through(
        self, mock_get_client: MagicMock, config: LLMJudgeConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = TimeoutError("API timeout")
        mock_get_client.return_value = mock_client
        judge = LLMJudge(config)
        result = judge.judge("test")
        assert result is not None
        assert result.is_injection is False


class TestUsageTracking:
    @patch("creel.llm._get_client")
    def test_usage_stats_accumulate(
        self, mock_get_client: MagicMock, config: LLMJudgeConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"is_injection": false, "confidence": 0.1, "reasoning": "ok"}',
            input_tokens=150,
            output_tokens=30,
        )
        mock_get_client.return_value = mock_client
        judge = LLMJudge(config)

        judge.judge("test1")
        judge.judge("test2")

        stats = judge.usage_stats
        assert stats["calls"] == 2
        assert stats["input_tokens"] == 300
        assert stats["output_tokens"] == 60

    def test_initial_stats_zero(self, config: LLMJudgeConfig) -> None:
        judge = LLMJudge(config)
        stats = judge.usage_stats
        assert stats["calls"] == 0
        assert stats["input_tokens"] == 0


class TestDefaultConfig:
    def test_judge_enabled_by_default(self) -> None:
        config = LLMJudgeConfig()
        assert config.enabled is True

    def test_uncertain_only_by_default(self) -> None:
        config = LLMJudgeConfig()
        assert config.uncertain_only is True
