"""Tests for the agent loop."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from taskrunner.agent import AgentResult, run_agent_loop
from taskrunner.models import AgentConfig, LLMConfig, ToolConfig, ToolParameter


def _make_llm_config() -> LLMConfig:
    return LLMConfig(model="claude-sonnet-4-20250514", max_tokens=1024)


def _make_tools() -> dict[str, ToolConfig]:
    return {
        "check_weather": ToolConfig(
            fetcher="weather",
            description="Get weather",
            parameters={
                "location": ToolParameter(
                    type="string",
                    description="City",
                    required=True,
                ),
            },
        ),
    }


def _text_message(text: str) -> MagicMock:
    """Create a mock Anthropic Message with text-only content."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    msg.stop_reason = "end_turn"
    return msg


def _tool_use_message(tool_name: str, tool_input: dict, tool_id: str = "toolu_1") -> MagicMock:
    """Create a mock Anthropic Message with a tool_use block."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.input = tool_input
    tool_block.id = tool_id
    msg = MagicMock()
    msg.content = [tool_block]
    msg.stop_reason = "tool_use"
    return msg


@patch("taskrunner.agent.call_llm")
def test_simple_text_response(mock_call_llm):
    """Agent should return immediately when LLM gives a text response."""
    mock_call_llm.return_value = _text_message("Hello!")

    result = run_agent_loop(
        messages=[{"role": "user", "content": "Hi"}],
        llm_config=_make_llm_config(),
        tools_config={},
        agent_config=AgentConfig(max_turns=5),
    )

    assert result.text == "Hello!"
    assert result.turns_used == 1
    assert result.tool_calls_made == 0
    assert result.stop_reason == "end_turn"


@patch("taskrunner.agent.execute_tool_call")
@patch("taskrunner.agent.call_llm")
def test_tool_call_then_response(mock_call_llm, mock_execute):
    """Agent should execute tool, then return final text."""
    # First call: tool use. Second call: text response.
    mock_call_llm.side_effect = [
        _tool_use_message("check_weather", {"location": "Denver"}),
        _text_message("It's sunny in Denver!"),
    ]
    mock_execute.return_value = '{"temp_f": "72", "condition": "sunny"}'

    result = run_agent_loop(
        messages=[{"role": "user", "content": "Weather in Denver?"}],
        llm_config=_make_llm_config(),
        tools_config=_make_tools(),
        agent_config=AgentConfig(max_turns=5),
    )

    assert result.text == "It's sunny in Denver!"
    assert result.turns_used == 2
    assert result.tool_calls_made == 1
    assert result.stop_reason == "end_turn"
    assert len(result.tool_history) == 1
    assert result.tool_history[0]["tool"] == "check_weather"
    assert result.tool_history[0]["is_error"] is False


@patch("taskrunner.agent.execute_tool_call")
@patch("taskrunner.agent.call_llm")
def test_tool_error_continues(mock_call_llm, mock_execute):
    """Tool error should be passed back to LLM, not crash the loop."""
    mock_call_llm.side_effect = [
        _tool_use_message("check_weather", {"location": "Mars"}),
        _text_message("Sorry, I couldn't check the weather for Mars."),
    ]
    mock_execute.side_effect = RuntimeError("Location not found")

    result = run_agent_loop(
        messages=[{"role": "user", "content": "Weather on Mars?"}],
        llm_config=_make_llm_config(),
        tools_config=_make_tools(),
        agent_config=AgentConfig(max_turns=5),
    )

    assert result.text == "Sorry, I couldn't check the weather for Mars."
    assert result.tool_calls_made == 1
    assert result.tool_history[0]["is_error"] is True


@patch("taskrunner.agent.execute_tool_call")
@patch("taskrunner.agent.call_llm")
def test_max_turns_forces_summary(mock_call_llm, mock_execute):
    """When max_turns is reached, agent should force a final text response."""
    # Every turn returns a tool call - never voluntarily stops
    mock_call_llm.side_effect = [
        _tool_use_message("check_weather", {"location": "A"}, "t1"),
        _tool_use_message("check_weather", {"location": "B"}, "t2"),
        # Final forced call (no tools) returns text
        _text_message("I checked the weather for A and B."),
    ]
    mock_execute.return_value = '{"temp": "70"}'

    result = run_agent_loop(
        messages=[{"role": "user", "content": "Check weather everywhere"}],
        llm_config=_make_llm_config(),
        tools_config=_make_tools(),
        agent_config=AgentConfig(max_turns=2),
    )

    assert result.stop_reason == "max_turns"
    assert result.turns_used == 2
    assert result.tool_calls_made == 2
    assert "checked the weather" in result.text


@patch("taskrunner.agent.call_llm")
def test_llm_error_returns_error_result(mock_call_llm):
    """LLM call failure should return an error AgentResult."""
    mock_call_llm.side_effect = RuntimeError("API rate limited")

    result = run_agent_loop(
        messages=[{"role": "user", "content": "Hi"}],
        llm_config=_make_llm_config(),
        tools_config={},
        agent_config=AgentConfig(max_turns=5),
    )

    assert result.stop_reason == "error"
    assert "API rate limited" in result.text


@patch("taskrunner.agent.call_llm")
def test_no_tools_configured(mock_call_llm):
    """Agent should work fine with no tools (pure chat)."""
    mock_call_llm.return_value = _text_message("Just chatting!")

    result = run_agent_loop(
        messages=[{"role": "user", "content": "Tell me a joke"}],
        llm_config=_make_llm_config(),
        tools_config={},
        agent_config=AgentConfig(max_turns=5),
    )

    assert result.text == "Just chatting!"
    assert result.tool_calls_made == 0

    # Verify tools=None was passed to call_llm
    call_kwargs = mock_call_llm.call_args
    assert call_kwargs.kwargs.get("tools") is None or call_kwargs[1].get("tools") is None


@patch("taskrunner.agent.call_llm")
def test_system_prompt_passed(mock_call_llm):
    """System prompt should be forwarded to call_llm."""
    mock_call_llm.return_value = _text_message("Ok")

    run_agent_loop(
        messages=[{"role": "user", "content": "Hi"}],
        llm_config=_make_llm_config(),
        tools_config={},
        agent_config=AgentConfig(max_turns=5),
        system_prompt="You are helpful.",
    )

    call_kwargs = mock_call_llm.call_args
    assert call_kwargs.kwargs.get("system") == "You are helpful."
