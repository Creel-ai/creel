"""Tests for tool definition building and execution."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from creel.models import SkillOverride
from creel.skills.registry import SkillRegistry
from creel.tools import build_tool_definitions, execute_tool_call


def _make_registry() -> SkillRegistry:
    """Build a registry with all built-in skills discovered."""
    registry = SkillRegistry()
    registry._discover_builtins()
    return registry


def _make_overrides() -> dict[str, SkillOverride]:
    """Build skill overrides that include weather and gmail_modify."""
    return {
        "weather": SkillOverride(enabled=True),
        "gmail_modify": SkillOverride(enabled=True),
    }


def test_build_tool_definitions():
    """Tool definitions should match Anthropic API format."""
    registry = _make_registry()
    overrides = _make_overrides()
    defs = build_tool_definitions(registry, overrides)

    assert len(defs) > 0

    # Weather skill should expose check_weather or weather tool
    weather_names = [d["name"] for d in defs if "weather" in d["name"].lower()]
    assert len(weather_names) > 0

    # Fixed args should NOT appear as parameters in any tool
    for d in defs:
        props = d["input_schema"].get("properties", {})
        # fixed_args like 'action' should not be in properties
        if d["name"] in ("trash_email", "delete_message"):
            assert "action" not in props


def test_build_tool_definitions_empty():
    """Empty skill overrides should return empty list."""
    registry = _make_registry()
    assert build_tool_definitions(registry, {}) == []


def test_build_tool_definitions_no_required():
    """Tools with no required params should have no 'required' key or empty list."""
    registry = _make_registry()
    overrides = {"drive": SkillOverride(enabled=True)}
    defs = build_tool_definitions(registry, overrides)
    # Find a tool that has no required params
    for d in defs:
        if not d["input_schema"].get("required"):
            # Either no 'required' key, or empty list
            assert "required" not in d["input_schema"] or d["input_schema"]["required"] == []
            break


@patch("creel.tools._run_executor_inline_skill")
def test_execute_tool_call_merges_fixed_args(mock_run):
    """fixed_args should override LLM input."""
    mock_run.return_value = '{"status": "trashed"}'
    registry = _make_registry()
    overrides = {"gmail_modify": SkillOverride(enabled=True)}

    result = execute_tool_call(
        tool_name="trash_email",
        tool_input={"message_id": "abc123"},
        registry=registry,
        skill_overrides=overrides,
    )

    assert result == '{"status": "trashed"}'

    # Verify the executor was called with merged args
    call_args = mock_run.call_args
    executor_config = call_args[0][2]  # 3rd positional arg is config
    assert executor_config.args["message_id"] == "abc123"
    assert executor_config.args["action"] == "trash"


@patch("creel.tools._run_executor_inline_skill")
def test_execute_tool_call_fixed_args_win(mock_run):
    """If LLM tries to override a fixed_arg, the fixed value wins."""
    mock_run.return_value = '{"ok": true}'
    registry = _make_registry()
    overrides = {"gmail_modify": SkillOverride(enabled=True)}

    execute_tool_call(
        tool_name="trash_email",
        tool_input={"message_id": "abc", "action": "delete"},
        registry=registry,
        skill_overrides=overrides,
    )

    executor_config = mock_run.call_args[0][2]
    assert executor_config.args["action"] == "trash"  # fixed wins


def test_execute_tool_call_unknown_tool():
    """Unknown tool should raise ValueError."""
    registry = _make_registry()
    with pytest.raises(ValueError, match="Unknown tool"):
        execute_tool_call("nonexistent", {}, registry, {})
