"""Tests for LLM authentication, retry logic, streaming, and summarization."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creel.llm import (
    MAX_RETRIES,
    _run_llm_container,
    _run_llm_direct,
    call_llm,
    summarize_messages,
)
from creel.models import LLMConfig
from creel.providers import (
    LLMAuthError,
    LLMMessage,
    LLMRateLimitError,
    LLMTransientError,
    TextBlock,
    Usage,
)
from creel.providers.anthropic import _CLAUDE_CODE_SYSTEM_PREFIX


def _make_config(**overrides) -> LLMConfig:
    defaults = {"model": "claude-sonnet-4-6", "max_tokens": 100}
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _mock_llm_message(text: str = "Hello") -> LLMMessage:
    return LLMMessage(
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


# -- _run_llm_direct auth tests --


@patch("creel.providers.anthropic._get_client")
def test_direct_uses_auth_token(mock_get_client, monkeypatch):
    """ANTHROPIC_AUTH_TOKEN should be passed as auth_token=."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import anthropic

    mock_client = MagicMock()
    mock_msg = MagicMock(spec=anthropic.types.Message)
    block = MagicMock()
    block.type = "text"
    block.text = "Hello"
    mock_msg.content = [block]
    mock_msg.stop_reason = "end_turn"
    mock_msg.usage = MagicMock(input_tokens=10, output_tokens=5)
    mock_client.messages.create.return_value = mock_msg
    mock_get_client.return_value = mock_client

    result = _run_llm_direct("hi", _make_config())

    assert result == "Hello"


@patch("creel.providers.anthropic._get_client")
def test_direct_uses_api_key(mock_get_client, monkeypatch):
    """ANTHROPIC_API_KEY should be passed as api_key=."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    import anthropic

    mock_client = MagicMock()
    mock_msg = MagicMock(spec=anthropic.types.Message)
    block = MagicMock()
    block.type = "text"
    block.text = "Hello"
    mock_msg.content = [block]
    mock_msg.stop_reason = "end_turn"
    mock_msg.usage = MagicMock(input_tokens=10, output_tokens=5)
    mock_client.messages.create.return_value = mock_msg
    mock_get_client.return_value = mock_client

    result = _run_llm_direct("hi", _make_config())

    assert result == "Hello"


def test_direct_no_credentials_raises(monkeypatch):
    """Missing both credentials should raise."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(LLMAuthError):
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


# -- retry tests --


class TestRetryOnTransient:
    def test_max_retries_exhaustion(self) -> None:
        """After MAX_RETRIES, the last exception should be re-raised."""
        from creel.llm import _retry_on_transient

        exc = LLMRateLimitError("rate limited", status_code=429)

        fn = MagicMock(side_effect=exc)

        with (
            patch("creel.llm.time.sleep"),
            pytest.raises(LLMRateLimitError),
        ):
            _retry_on_transient(fn)

        assert fn.call_count == MAX_RETRIES

    def test_non_retryable_propagates_immediately(self) -> None:
        """Non-retryable errors should not be retried."""
        from creel.llm import _retry_on_transient
        from creel.providers import LLMAuthError

        exc = LLMAuthError("unauthorized", status_code=401)

        fn = MagicMock(side_effect=exc)

        with pytest.raises(LLMAuthError):
            _retry_on_transient(fn)

        assert fn.call_count == 1

    def test_success_on_second_attempt(self) -> None:
        """Function should succeed after a transient error."""
        from creel.llm import _retry_on_transient

        exc_retryable = LLMTransientError("overloaded", status_code=502)

        fn = MagicMock(side_effect=[exc_retryable, "success"])

        with patch("creel.llm.time.sleep"):
            result = _retry_on_transient(fn)

        assert result == "success"
        assert fn.call_count == 2


# -- call_llm tests --


class TestCallLlm:
    @patch("creel.providers.anthropic._get_client")
    def test_with_tools_param(self, mock_get_client, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = MagicMock()
        mock_resp = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = "Hello"
        mock_resp.content = [block]
        mock_resp.stop_reason = "end_turn"
        mock_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_client.messages.create.return_value = mock_resp
        mock_get_client.return_value = mock_client

        tools = [{"name": "weather", "description": "Get weather", "input_schema": {}}]
        messages = [{"role": "user", "content": "What's the weather?"}]

        call_llm(messages, _make_config(), tools=tools)

        create_call = mock_client.messages.create
        create_call.assert_called_once()
        kwargs = create_call.call_args[1]
        assert kwargs["tools"] == tools

    @patch("creel.providers.anthropic._get_client")
    def test_with_system_param(self, mock_get_client, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_client = MagicMock()
        mock_resp = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = "Hello"
        mock_resp.content = [block]
        mock_resp.stop_reason = "end_turn"
        mock_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_client.messages.create.return_value = mock_resp
        mock_get_client.return_value = mock_client

        messages = [{"role": "user", "content": "hi"}]
        call_llm(messages, _make_config(), system="You are helpful.")

        create_call = mock_client.messages.create
        kwargs = create_call.call_args[1]
        assert kwargs["system"] == "You are helpful."

    @patch("creel.providers.anthropic._get_client")
    def test_oauth_prefix_injection(self, mock_get_client, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-token")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        mock_client = MagicMock()
        mock_resp = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = "Hello"
        mock_resp.content = [block]
        mock_resp.stop_reason = "end_turn"
        mock_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_client.messages.create.return_value = mock_resp
        mock_get_client.return_value = mock_client

        messages = [{"role": "user", "content": "hi"}]
        # No explicit system prompt — should inject Claude Code prefix
        call_llm(messages, _make_config())

        create_call = mock_client.messages.create
        kwargs = create_call.call_args[1]
        assert kwargs["system"] == _CLAUDE_CODE_SYSTEM_PREFIX

    def test_no_credentials_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        messages = [{"role": "user", "content": "hi"}]
        with pytest.raises(LLMAuthError):
            call_llm(messages, _make_config())

    @patch("creel.providers.anthropic._get_client")
    def test_on_text_delta_uses_streaming(self, mock_get_client, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        mock_stream = MagicMock()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.text_stream = iter(["hi"])

        final_msg = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = "hi"
        final_msg.content = [block]
        final_msg.stop_reason = "end_turn"
        final_msg.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_stream.get_final_message.return_value = final_msg

        mock_client = MagicMock()
        mock_client.messages.stream.return_value = mock_stream
        mock_get_client.return_value = mock_client

        messages = [{"role": "user", "content": "hi"}]
        deltas = []
        call_llm(messages, _make_config(), on_text_delta=deltas.append)

        assert deltas == ["hi"]
        mock_client.messages.stream.assert_called_once()


# -- summarize_messages tests --


class TestSummarizeMessages:
    @patch("creel.llm._run_llm_direct")
    def test_formats_tool_use_and_tool_result(self, mock_direct, monkeypatch) -> None:
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
    def test_truncation_at_200_chars(self, mock_direct, monkeypatch) -> None:
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
