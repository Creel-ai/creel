"""Tests for streaming LLM responses."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from creel.agent import run_agent_loop
from creel.llm import call_llm
from creel.models import AgentConfig, LLMConfig
from creel.providers import LLMMessage, TextBlock, Usage


def _make_config() -> LLMConfig:
    return LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100)


def _mock_llm_message(text: str = "Hello world") -> LLMMessage:
    return LLMMessage(
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=50, output_tokens=10),
    )


def _mock_anthropic_message() -> MagicMock:
    """Create a mock Anthropic SDK Message (for tests that mock the SDK directly)."""
    block = MagicMock()
    block.type = "text"
    block.text = "Hello world"
    msg = MagicMock()
    msg.content = [block]
    msg.stop_reason = "end_turn"
    msg.usage = MagicMock()
    msg.usage.input_tokens = 50
    msg.usage.output_tokens = 10
    return msg


def _make_mock_stream(chunks: list[str], final_message: MagicMock):
    """Create a mock stream context manager that yields chunks via text_stream."""
    stream = MagicMock()
    stream.text_stream = iter(chunks)
    stream.get_final_message.return_value = final_message
    stream.__enter__ = MagicMock(return_value=stream)
    stream.__exit__ = MagicMock(return_value=False)
    return stream


# -- Streaming via call_llm (provider-based) --


@patch("creel.providers.anthropic._get_client")
def test_streaming_calls_on_text_delta_for_each_chunk(mock_get_client, monkeypatch):
    """on_text_delta should be called once per chunk from text_stream."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    chunks = ["Hello", " ", "world"]
    final_msg = _mock_anthropic_message()
    stream = _make_mock_stream(chunks, final_msg)

    client = MagicMock()
    client.messages.stream.return_value = stream
    mock_get_client.return_value = client

    received: list[str] = []
    result = call_llm(
        messages=[{"role": "user", "content": "hello"}],
        config=_make_config(),
        on_text_delta=received.append,
    )

    assert received == chunks
    assert result.content[0].text == "Hello world"


@patch("creel.providers.anthropic._get_client")
def test_streaming_returns_llm_message(mock_get_client, monkeypatch):
    """Streaming should return an LLMMessage type."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    final_msg = _mock_anthropic_message()
    stream = _make_mock_stream(["ok"], final_msg)

    client = MagicMock()
    client.messages.stream.return_value = stream
    mock_get_client.return_value = client

    result = call_llm(
        messages=[{"role": "user", "content": "hello"}],
        config=_make_config(),
        on_text_delta=lambda _: None,
    )

    assert isinstance(result, LLMMessage)
    assert result.content[0].type == "text"


# -- Streaming fallback tests --


@patch("creel.providers.anthropic._get_client")
def test_streaming_falls_back_on_transient_error(mock_get_client, monkeypatch):
    """Streaming should fall back to non-streaming on retryable errors."""
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    resp = MagicMock()
    resp.status_code = 503
    resp.headers = {}

    client = MagicMock()
    client.messages.stream.side_effect = anthropic.APIStatusError(
        message="overloaded", response=resp, body=None
    )
    # Fallback to create
    final_msg = _mock_anthropic_message()
    client.messages.create.return_value = final_msg
    mock_get_client.return_value = client

    received: list[str] = []
    with patch("creel.llm.time.sleep"):
        result = call_llm(
            messages=[{"role": "user", "content": "hello"}],
            config=_make_config(),
            on_text_delta=received.append,
        )

    client.messages.create.assert_called()
    assert result.content[0].text == "Hello world"
    assert received == []


@patch("creel.providers.anthropic._get_client")
def test_streaming_reraises_non_retryable_error(mock_get_client, monkeypatch):
    """Streaming should reraise non-retryable API errors."""
    import anthropic
    import pytest

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    resp = MagicMock()
    resp.status_code = 400
    resp.headers = {}

    client = MagicMock()
    client.messages.stream.side_effect = anthropic.APIStatusError(
        message="bad request", response=resp, body=None
    )
    mock_get_client.return_value = client

    with pytest.raises(Exception):
        call_llm(
            messages=[{"role": "user", "content": "hello"}],
            config=_make_config(),
            on_text_delta=lambda _: None,
        )


# -- call_llm streaming vs non-streaming dispatch --


@patch("creel.providers.anthropic._get_client")
def test_call_llm_uses_stream_when_callback_provided(mock_get_client, monkeypatch):
    """call_llm should use streaming when on_text_delta is set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    chunks = ["Hi", " there"]
    final_msg = _mock_anthropic_message()
    stream = _make_mock_stream(chunks, final_msg)

    client = MagicMock()
    client.messages.stream.return_value = stream
    mock_get_client.return_value = client

    received: list[str] = []
    result = call_llm(
        messages=[{"role": "user", "content": "hello"}],
        config=_make_config(),
        on_text_delta=received.append,
    )

    assert isinstance(result, LLMMessage)
    assert received == chunks
    # Should NOT have called .create
    client.messages.create.assert_not_called()


@patch("creel.providers.anthropic._get_client")
def test_call_llm_uses_create_when_no_callback(mock_get_client, monkeypatch):
    """call_llm should use create() when on_text_delta is None."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    client = MagicMock()
    client.messages.create.return_value = _mock_anthropic_message()
    mock_get_client.return_value = client

    result = call_llm(
        messages=[{"role": "user", "content": "hello"}],
        config=_make_config(),
    )

    client.messages.create.assert_called_once()
    client.messages.stream.assert_not_called()
    assert result.content[0].text == "Hello world"


# -- Agent loop passes callback through --


@patch("creel.agent.call_llm")
def test_agent_loop_passes_on_text_delta(mock_call_llm):
    """run_agent_loop should forward on_text_delta to call_llm."""
    mock_call_llm.return_value = _mock_llm_message()

    callback = MagicMock()
    result = run_agent_loop(
        messages=[{"role": "user", "content": "hi"}],
        llm_config=_make_config(),
        tools_config={},
        agent_config=AgentConfig(max_turns=1),
        on_text_delta=callback,
    )

    assert result.stop_reason == "end_turn"
    # Verify the callback was passed to call_llm
    _, kwargs = mock_call_llm.call_args
    assert kwargs["on_text_delta"] is callback


@patch("creel.agent.call_llm")
def test_agent_loop_none_callback_by_default(mock_call_llm):
    """run_agent_loop should pass on_text_delta=None when not provided."""
    mock_call_llm.return_value = _mock_llm_message()

    result = run_agent_loop(
        messages=[{"role": "user", "content": "hi"}],
        llm_config=_make_config(),
        tools_config={},
        agent_config=AgentConfig(max_turns=1),
    )

    assert result.stop_reason == "end_turn"
    _, kwargs = mock_call_llm.call_args
    assert kwargs["on_text_delta"] is None


# -- Streaming with empty chunks --


@patch("creel.providers.anthropic._get_client")
def test_streaming_handles_empty_chunks(mock_get_client, monkeypatch):
    """Streaming should gracefully handle empty string chunks."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    chunks = ["", "Hello", "", " world", ""]
    final_msg = _mock_anthropic_message()
    stream = _make_mock_stream(chunks, final_msg)

    client = MagicMock()
    client.messages.stream.return_value = stream
    mock_get_client.return_value = client

    received: list[str] = []
    result = call_llm(
        messages=[{"role": "user", "content": "hello"}],
        config=_make_config(),
        on_text_delta=received.append,
    )

    assert received == chunks
    assert isinstance(result, LLMMessage)
