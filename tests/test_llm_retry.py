"""Tests for LLM retry with exponential backoff."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

from creel.llm import (
    _retry_on_transient,
    _run_llm_direct,
    call_llm,
)
from creel.models import LLMConfig


def _make_config() -> LLMConfig:
    return LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100)


def _mock_message() -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = "Hello"
    msg = MagicMock()
    msg.content = [block]
    return msg


def _make_api_error(status_code: int) -> anthropic.APIStatusError:
    resp = httpx.Response(status_code, request=httpx.Request("POST", "https://api.anthropic.com"))
    return anthropic.APIStatusError(
        message=f"Error {status_code}",
        response=resp,
        body=None,
    )


class TestRetryOnTransient:
    """Tests for the _retry_on_transient wrapper."""

    @patch("creel.llm.time.sleep")
    def test_retries_on_429(self, mock_sleep):
        fn = MagicMock(side_effect=[_make_api_error(429), "ok"])
        result = _retry_on_transient(fn, "arg1")
        assert result == "ok"
        assert fn.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    @patch("creel.llm.time.sleep")
    def test_retries_on_500(self, mock_sleep):
        fn = MagicMock(side_effect=[_make_api_error(500), "ok"])
        result = _retry_on_transient(fn, "arg1")
        assert result == "ok"
        mock_sleep.assert_called_once_with(1.0)

    @patch("creel.llm.time.sleep")
    def test_retries_on_502(self, mock_sleep):
        fn = MagicMock(side_effect=[_make_api_error(502), "ok"])
        result = _retry_on_transient(fn)
        assert result == "ok"

    @patch("creel.llm.time.sleep")
    def test_retries_on_503(self, mock_sleep):
        fn = MagicMock(side_effect=[_make_api_error(503), "ok"])
        result = _retry_on_transient(fn)
        assert result == "ok"

    @patch("creel.llm.time.sleep")
    def test_exponential_backoff_delays(self, mock_sleep):
        """Should use 1s, 2s delays for 3 attempts."""
        fn = MagicMock(
            side_effect=[
                _make_api_error(429),
                _make_api_error(429),
                "ok",
            ]
        )
        result = _retry_on_transient(fn)
        assert result == "ok"
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)

    @patch("creel.llm.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        fn = MagicMock(
            side_effect=[
                _make_api_error(429),
                _make_api_error(429),
                _make_api_error(429),
            ]
        )
        with pytest.raises(anthropic.APIStatusError):
            _retry_on_transient(fn)
        assert fn.call_count == 3

    def test_no_retry_on_400(self):
        fn = MagicMock(side_effect=_make_api_error(400))
        with pytest.raises(anthropic.APIStatusError):
            _retry_on_transient(fn)
        assert fn.call_count == 1

    def test_no_retry_on_401(self):
        fn = MagicMock(side_effect=_make_api_error(401))
        with pytest.raises(anthropic.APIStatusError):
            _retry_on_transient(fn)
        assert fn.call_count == 1

    @patch("creel.llm.time.sleep")
    def test_success_on_first_try(self, mock_sleep):
        fn = MagicMock(return_value="ok")
        result = _retry_on_transient(fn, "a", b="c")
        assert result == "ok"
        fn.assert_called_once_with("a", b="c")
        mock_sleep.assert_not_called()


class TestCallLlmRetry:
    """Verify call_llm and _run_llm_direct use retry logic."""

    @patch("creel.llm.time.sleep")
    @patch("creel.llm.anthropic.Anthropic")
    def test_call_llm_retries_on_transient(self, mock_cls, mock_sleep, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = mock_cls.return_value
        mock_client.messages.create.side_effect = [
            _make_api_error(429),
            _mock_message(),
        ]

        result = call_llm(
            messages=[{"role": "user", "content": "hi"}],
            config=_make_config(),
        )
        assert result.content[0].text == "Hello"
        assert mock_client.messages.create.call_count == 2

    @patch("creel.llm.time.sleep")
    @patch("creel.llm.anthropic.Anthropic")
    def test_run_llm_direct_retries(self, mock_cls, mock_sleep, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = mock_cls.return_value
        mock_client.messages.create.side_effect = [
            _make_api_error(502),
            _mock_message(),
        ]

        result = _run_llm_direct("hello", _make_config())
        assert result == "Hello"
        assert mock_client.messages.create.call_count == 2
