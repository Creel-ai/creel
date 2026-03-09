"""Tests for LLM authentication, retry logic, streaming, and summarization."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import anthropic
import pytest

from creel.llm import (
    _CLAUDE_CODE_SYSTEM_PREFIX,
    _OAUTH_HEADERS,
    MAX_RETRIES,
    _call_llm_streaming,
    _retry_on_transient,
    _run_llm_container,
    _run_llm_direct,
    call_llm,
    summarize_messages,
)
from creel.models import LLMConfig


def _make_config() -> LLMConfig:
    return LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100)


def _mock_message(text: str = "Hello") -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    return msg


# -- _run_llm_direct auth tests --


@patch("creel.llm.anthropic.Anthropic")
def test_direct_uses_auth_token(mock_cls, monkeypatch):
    """ANTHROPIC_AUTH_TOKEN should be passed as auth_token=."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    mock_cls.return_value.messages.create.return_value = _mock_message()

    result = _run_llm_direct("hi", _make_config())

    mock_cls.assert_called_once_with(
        auth_token="sk-ant-oat01-test",
        default_headers=_OAUTH_HEADERS,
    )
    assert result == "Hello"


@patch("creel.llm.anthropic.Anthropic")
def test_direct_uses_api_key(mock_cls, monkeypatch):
    """ANTHROPIC_API_KEY should be passed as api_key=."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    mock_cls.return_value.messages.create.return_value = _mock_message()

    result = _run_llm_direct("hi", _make_config())

    mock_cls.assert_called_once_with(api_key="sk-ant-test-key")
    assert result == "Hello"


@patch("creel.llm.anthropic.Anthropic")
def test_direct_auth_token_takes_precedence(mock_cls, monkeypatch):
    """When both are set, auth_token wins."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-key")

    mock_cls.return_value.messages.create.return_value = _mock_message()

    _run_llm_direct("hi", _make_config())

    mock_cls.assert_called_once_with(
        auth_token="sk-ant-oat01-token",
        default_headers=_OAUTH_HEADERS,
    )


@patch("creel.llm.anthropic.Anthropic")
def test_direct_non_oauth_auth_token_no_headers(mock_cls, monkeypatch):
    """A non-OAuth auth_token should not get the spoofed headers."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-other-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    mock_cls.return_value.messages.create.return_value = _mock_message()

    _run_llm_direct("hi", _make_config())

    mock_cls.assert_called_once_with(
        auth_token="sk-ant-other-token",
        default_headers={},
    )


def test_direct_no_credentials_raises(monkeypatch):
    """Missing both credentials should raise RuntimeError."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="No Anthropic credentials found"):
        _run_llm_direct("hi", _make_config())


# -- _run_llm_container auth tests --


@patch("creel.containers._ensure_image")
@patch("creel.llm.subprocess.run")
def test_container_passes_auth_token(mock_run, _mock_ensure, monkeypatch, tmp_path):
    """ANTHROPIC_AUTH_TOKEN should be written to the env file."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Use a real temp file in tmp_path so we can read it back
    env_file = tmp_path / "test.env"
    mock_run.return_value = MagicMock(stdout="response", stderr="", returncode=0)

    with patch("creel.llm.tempfile.NamedTemporaryFile", return_value=open(env_file, "w+")):
        _run_llm_container("hi", _make_config())

    cmd = mock_run.call_args[0][0]
    assert "--env-file" in cmd
    contents = env_file.read_text()
    assert "ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-token" in contents


@patch("creel.containers._ensure_image")
@patch("creel.llm.subprocess.run")
def test_container_passes_api_key(mock_run, _mock_ensure, monkeypatch, tmp_path):
    """ANTHROPIC_API_KEY should be written to the env file."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")

    env_file = tmp_path / "test.env"
    mock_run.return_value = MagicMock(stdout="response", stderr="", returncode=0)

    with patch("creel.llm.tempfile.NamedTemporaryFile", return_value=open(env_file, "w+")):
        _run_llm_container("hi", _make_config())

    contents = env_file.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-key" in contents


@patch("creel.containers._ensure_image")
@patch("creel.llm.subprocess.run")
def test_container_passes_both_when_set(mock_run, _mock_ensure, monkeypatch, tmp_path):
    """Both env vars should be written to the env file."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")

    env_file = tmp_path / "test.env"
    mock_run.return_value = MagicMock(stdout="response", stderr="", returncode=0)

    with patch("creel.llm.tempfile.NamedTemporaryFile", return_value=open(env_file, "w+")):
        _run_llm_container("hi", _make_config())

    contents = env_file.read_text()
    assert "ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-token" in contents
    assert "ANTHROPIC_API_KEY=sk-ant-key" in contents


# -- _retry_on_transient tests --


class TestRetryOnTransient:
    def test_max_retries_exhaustion(self) -> None:
        """After MAX_RETRIES, the last exception should be re-raised."""
        exc = anthropic.APIStatusError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body=None,
        )

        fn = MagicMock(side_effect=exc)

        with (
            patch("creel.llm.time.sleep"),
            pytest.raises(anthropic.APIStatusError),
        ):
            _retry_on_transient(fn)

        assert fn.call_count == MAX_RETRIES

    def test_non_retryable_propagates_immediately(self) -> None:
        """Non-retryable errors (e.g. 401) should not be retried."""
        exc = anthropic.APIStatusError(
            message="unauthorized",
            response=MagicMock(status_code=401),
            body=None,
        )

        fn = MagicMock(side_effect=exc)

        with pytest.raises(anthropic.APIStatusError):
            _retry_on_transient(fn)

        assert fn.call_count == 1

    def test_success_on_second_attempt(self) -> None:
        """Function should succeed after a transient error."""
        anthropic.APIStatusError(
            message="overloaded",
            response=MagicMock(status_code=529),
            body=None,
        )
        # First call raises retryable (we need to use a retryable code)
        exc_retryable = anthropic.APIStatusError(
            message="overloaded",
            response=MagicMock(status_code=502),
            body=None,
        )

        fn = MagicMock(side_effect=[exc_retryable, "success"])

        with patch("creel.llm.time.sleep"):
            result = _retry_on_transient(fn)

        assert result == "success"
        assert fn.call_count == 2


# -- _call_llm_streaming tests --


class TestCallLlmStreaming:
    def test_text_delta_callback_invoked(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        chunks = ["Hello ", "world!"]
        mock_stream = MagicMock()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.text_stream = iter(chunks)
        mock_stream.get_final_message.return_value = _mock_message("Hello world!")

        mock_client = MagicMock()
        mock_client.messages.stream.return_value = mock_stream

        collected = []
        result = _call_llm_streaming(
            mock_client, {"model": "test", "max_tokens": 100, "messages": []}, collected.append
        )

        assert collected == ["Hello ", "world!"]
        assert result.content[0].text == "Hello world!"

    def test_transient_error_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        exc = anthropic.APIStatusError(
            message="overloaded",
            response=MagicMock(status_code=502),
            body=None,
        )

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = exc
        mock_client.messages.create.return_value = _mock_message("fallback")

        with patch("creel.llm.time.sleep"):
            result = _call_llm_streaming(
                mock_client,
                {"model": "test", "max_tokens": 100, "messages": []},
                lambda x: None,
            )

        assert result.content[0].text == "fallback"


# -- call_llm tests --


class TestCallLlm:
    @patch("creel.llm.anthropic.Anthropic")
    def test_with_tools_param(self, mock_cls, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_cls.return_value.messages.create.return_value = _mock_message()

        tools = [{"name": "weather", "description": "Get weather", "input_schema": {}}]
        messages = [{"role": "user", "content": "What's the weather?"}]

        call_llm(messages, _make_config(), tools=tools)

        create_call = mock_cls.return_value.messages.create
        create_call.assert_called_once()
        kwargs = create_call.call_args[1]
        assert kwargs["tools"] == tools

    @patch("creel.llm.anthropic.Anthropic")
    def test_with_system_param(self, mock_cls, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_cls.return_value.messages.create.return_value = _mock_message()

        messages = [{"role": "user", "content": "hi"}]
        call_llm(messages, _make_config(), system="You are helpful.")

        create_call = mock_cls.return_value.messages.create
        kwargs = create_call.call_args[1]
        assert kwargs["system"] == "You are helpful."

    @patch("creel.llm.anthropic.Anthropic")
    def test_oauth_prefix_injection(self, mock_cls, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-token")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_cls.return_value.messages.create.return_value = _mock_message()

        messages = [{"role": "user", "content": "hi"}]
        # No explicit system prompt — should inject Claude Code prefix
        call_llm(messages, _make_config())

        create_call = mock_cls.return_value.messages.create
        kwargs = create_call.call_args[1]
        assert kwargs["system"] == _CLAUDE_CODE_SYSTEM_PREFIX

    def test_no_credentials_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        messages = [{"role": "user", "content": "hi"}]
        with pytest.raises(RuntimeError, match="No Anthropic credentials"):
            call_llm(messages, _make_config())

    @patch("creel.llm.anthropic.Anthropic")
    def test_on_text_delta_uses_streaming(self, mock_cls, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_stream = MagicMock()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.text_stream = iter(["hi"])
        mock_stream.get_final_message.return_value = _mock_message("hi")

        mock_cls.return_value.messages.stream.return_value = mock_stream

        messages = [{"role": "user", "content": "hi"}]
        deltas = []
        call_llm(messages, _make_config(), on_text_delta=deltas.append)

        assert deltas == ["hi"]
        mock_cls.return_value.messages.stream.assert_called_once()


# -- summarize_messages tests --


class TestSummarizeMessages:
    @patch("creel.llm._run_llm_direct")
    @patch("creel.llm._get_client")
    def test_formats_tool_use_and_tool_result(
        self, mock_get_client, mock_direct, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        mock_direct.return_value = "Summary of conversation"

        messages = [
            {"role": "user", "content": "What's the weather?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "weather", "input": {"location": "Denver"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "Sunny, 72F"},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "It's sunny!"}]},
        ]

        result = summarize_messages(messages)
        assert result == "Summary of conversation"

        # Check the prompt that was passed to run_llm
        prompt = mock_direct.call_args[0][0]
        assert "weather" in prompt
        assert "Denver" in prompt
        assert "Sunny" in prompt

    @patch("creel.llm._run_llm_direct")
    @patch("creel.llm._get_client")
    def test_truncation_at_200_chars(self, mock_get_client, mock_direct, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        mock_direct.return_value = "summary"

        long_content = "x" * 300
        messages = [
            {
                "role": "user",
                "content": [{"type": "tool_result", "content": long_content}],
            },
        ]

        summarize_messages(messages)
        prompt = mock_direct.call_args[0][0]
        # The tool_result content should be truncated at 200 chars + "..."
        assert "..." in prompt
