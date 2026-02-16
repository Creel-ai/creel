"""Tests for streaming LLM responses."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from taskrunner.agent import run_agent_loop
from taskrunner.llm import _call_llm_streaming, call_llm
from taskrunner.models import AgentConfig, LLMConfig, ToolConfig, ToolParameter


def _make_config() -> LLMConfig:
    return LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100)


def _mock_message() -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = "Hello world"
    msg = MagicMock()
    msg.content = [block]
    msg.stop_reason = "end_turn"
    msg.usage = MagicMock()
    msg.usage.input_tokens = 50
    return msg


def _make_mock_stream(chunks: list[str], final_message: MagicMock):
    """Create a mock stream context manager that yields chunks via text_stream."""
    stream = MagicMock()
    stream.text_stream = iter(chunks)
    stream.get_final_message.return_value = final_message
    stream.__enter__ = MagicMock(return_value=stream)
    stream.__exit__ = MagicMock(return_value=False)
    return stream


# -- _call_llm_streaming tests --


def test_streaming_calls_on_text_delta_for_each_chunk():
    """on_text_delta should be called once per chunk from text_stream."""
    chunks = ["Hello", " ", "world"]
    final_msg = _mock_message()
    stream = _make_mock_stream(chunks, final_msg)

    client = MagicMock()
    client.messages.stream.return_value = stream

    received: list[str] = []
    result = _call_llm_streaming(client, {"model": "test"}, received.append)

    assert received == chunks
    assert result is final_msg


def test_streaming_returns_final_message():
    """_call_llm_streaming returns the same Message type as non-streaming."""
    final_msg = _mock_message()
    stream = _make_mock_stream(["ok"], final_msg)

    client = MagicMock()
    client.messages.stream.return_value = stream

    result = _call_llm_streaming(client, {"model": "test"}, lambda _: None)

    assert result is final_msg
    stream.get_final_message.assert_called_once()


# -- call_llm streaming vs non-streaming dispatch --


@patch("taskrunner.llm._get_client")
def test_call_llm_uses_stream_when_callback_provided(mock_get_client, monkeypatch):
    """call_llm should use client.messages.stream() when on_text_delta is set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    chunks = ["Hi", " there"]
    final_msg = _mock_message()
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

    assert result is final_msg
    assert received == chunks
    # Should NOT have called .create
    client.messages.create.assert_not_called()


@patch("taskrunner.llm._get_client")
def test_call_llm_uses_create_when_no_callback(mock_get_client, monkeypatch):
    """call_llm should use client.messages.create() when on_text_delta is None."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    client = MagicMock()
    client.messages.create.return_value = _mock_message()
    mock_get_client.return_value = client

    result = call_llm(
        messages=[{"role": "user", "content": "hello"}],
        config=_make_config(),
    )

    client.messages.create.assert_called_once()
    client.messages.stream.assert_not_called()
    assert result.content[0].text == "Hello world"


# -- Agent loop passes callback through --


@patch("taskrunner.agent.call_llm")
def test_agent_loop_passes_on_text_delta(mock_call_llm):
    """run_agent_loop should forward on_text_delta to call_llm."""
    mock_call_llm.return_value = _mock_message()

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


@patch("taskrunner.agent.call_llm")
def test_agent_loop_none_callback_by_default(mock_call_llm):
    """run_agent_loop should pass on_text_delta=None when not provided."""
    mock_call_llm.return_value = _mock_message()

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


def test_streaming_handles_empty_chunks():
    """Streaming should gracefully handle empty string chunks."""
    chunks = ["", "Hello", "", " world", ""]
    final_msg = _mock_message()
    stream = _make_mock_stream(chunks, final_msg)

    client = MagicMock()
    client.messages.stream.return_value = stream

    received: list[str] = []
    result = _call_llm_streaming(client, {"model": "test"}, received.append)

    assert received == chunks
    assert result is final_msg
