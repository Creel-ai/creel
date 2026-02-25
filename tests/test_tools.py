"""Tests for tool definition building and execution."""

from __future__ import annotations

from unittest.mock import patch

from creel.models import ToolConfig, ToolParameter
from creel.tools import build_tool_definitions, execute_tool_call


def _make_tools() -> dict[str, ToolConfig]:
    return {
        "check_weather": ToolConfig(
            executor="weather",
            description="Get weather for a location",
            parameters={
                "location": ToolParameter(
                    type="string",
                    description="City name",
                    required=True,
                ),
            },
        ),
        "trash_email": ToolConfig(
            executor="gmail_modify",
            secrets="secrets/gmail_modify.env.enc",
            description="Trash an email",
            parameters={
                "message_id": ToolParameter(
                    type="string",
                    description="Gmail message ID",
                    required=True,
                ),
            },
            fixed_args={"action": "trash"},
        ),
    }


def test_build_tool_definitions():
    """Tool definitions should match Anthropic API format."""
    tools = _make_tools()
    defs = build_tool_definitions(tools)

    assert len(defs) == 2

    weather_def = next(d for d in defs if d["name"] == "check_weather")
    assert weather_def["description"] == "Get weather for a location"
    assert "location" in weather_def["input_schema"]["properties"]
    assert weather_def["input_schema"]["required"] == ["location"]

    trash_def = next(d for d in defs if d["name"] == "trash_email")
    # fixed_args should NOT appear as parameters
    assert "action" not in trash_def["input_schema"]["properties"]
    assert "message_id" in trash_def["input_schema"]["properties"]


def test_build_tool_definitions_empty():
    """Empty tools config should return empty list."""
    assert build_tool_definitions({}) == []


def test_build_tool_definitions_no_required():
    """Tools with no required params should have no 'required' key."""
    tools = {
        "check_drive": ToolConfig(
            executor="drive",
            description="Search Drive",
            parameters={
                "query": ToolParameter(type="string", description="Search query"),
            },
        ),
    }
    defs = build_tool_definitions(tools)
    assert "required" not in defs[0]["input_schema"]


def test_fixed_args_excluded_from_schema():
    """Parameters that are in fixed_args should be excluded from the schema."""
    tools = {
        "mark_read": ToolConfig(
            executor="gmail_modify",
            description="Mark as read",
            parameters={
                "message_id": ToolParameter(type="string", required=True),
                "action": ToolParameter(type="string"),
                "remove_labels": ToolParameter(type="string"),
            },
            fixed_args={"action": "modify", "remove_labels": "UNREAD"},
        ),
    }
    defs = build_tool_definitions(tools)
    props = defs[0]["input_schema"]["properties"]
    assert "message_id" in props
    assert "action" not in props
    assert "remove_labels" not in props


@patch("creel.tools._run_executor_inline")
def test_execute_tool_call_merges_fixed_args(mock_fetch):
    """fixed_args should override LLM input."""
    mock_fetch.return_value = '{"status": "trashed"}'
    tools = _make_tools()

    result = execute_tool_call(
        tool_name="trash_email",
        tool_input={"message_id": "abc123"},
        tools_config=tools,
    )

    assert result == '{"status": "trashed"}'

    # Verify the executor was called with merged args
    call_args = mock_fetch.call_args
    executor_config = call_args[0][1]
    assert executor_config.args["message_id"] == "abc123"
    assert executor_config.args["action"] == "trash"


@patch("creel.tools._run_executor_inline")
def test_execute_tool_call_fixed_args_win(mock_fetch):
    """If LLM tries to override a fixed_arg, the fixed value wins."""
    mock_fetch.return_value = '{"ok": true}'
    tools = _make_tools()

    execute_tool_call(
        tool_name="trash_email",
        tool_input={"message_id": "abc", "action": "delete"},
        tools_config=tools,
    )

    executor_config = mock_fetch.call_args[0][1]
    assert executor_config.args["action"] == "trash"  # fixed wins


def test_execute_tool_call_unknown_tool():
    """Unknown tool should raise ValueError."""
    import pytest

    with pytest.raises(ValueError, match="Unknown tool"):
        execute_tool_call("nonexistent", {}, {})
