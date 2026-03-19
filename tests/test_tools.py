"""Tests for tool definition building and execution."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from creel.models import HttpConfig, SkillOverride, SkillToolOverride
from creel.skills.models import Param, SkillMeta, ToolSpec
from creel.skills.registry import SkillRegistry
from creel.tools import (
    _build_tool_config_from_skill,
    _execute_skill_tool,
    build_tool_definitions,
    execute_tool_call,
)


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


# ---------------------------------------------------------------------------
# Helpers for synthetic skill tests
# ---------------------------------------------------------------------------


def _make_simple_meta(
    skill_id: str = "my_skill",
    tool_name: str = "my_tool",
    needs_network: bool = False,
) -> SkillMeta:
    """Build a minimal SkillMeta with one tool."""
    return SkillMeta(
        id=skill_id,
        label=f"{skill_id} label",
        tools=(
            ToolSpec(
                name=tool_name,
                description=f"Do {tool_name}",
                params=(Param(name="query", type="string", description="A query", required=True),),
            ),
        ),
        needs_network=needs_network,
    )


def _make_registry_with(meta: SkillMeta) -> SkillRegistry:
    """Create a minimal registry containing *only* the given skill."""
    registry = SkillRegistry()
    registry.register(meta, lambda cfg: '{"ok": true}')
    return registry


# ---------------------------------------------------------------------------
# 1. Disabled skills excluded from build_tool_definitions
# ---------------------------------------------------------------------------


def test_disabled_skill_excluded_from_tool_definitions():
    """A skill with SkillOverride(enabled=False) must produce no tool definitions."""
    meta = _make_simple_meta("alpha", "alpha_tool")
    registry = _make_registry_with(meta)
    overrides = {"alpha": SkillOverride(enabled=False)}
    defs = build_tool_definitions(registry, overrides)
    assert defs == []


def test_disabled_skill_among_enabled():
    """Only enabled skills should emit tool definitions."""
    meta_a = _make_simple_meta("alpha", "alpha_tool")
    meta_b = _make_simple_meta("beta", "beta_tool")
    registry = SkillRegistry()
    registry.register(meta_a, lambda cfg: "")
    registry.register(meta_b, lambda cfg: "")

    overrides = {
        "alpha": SkillOverride(enabled=True),
        "beta": SkillOverride(enabled=False),
    }
    defs = build_tool_definitions(registry, overrides)
    names = [d["name"] for d in defs]
    assert "alpha_tool" in names
    assert "beta_tool" not in names


# ---------------------------------------------------------------------------
# 2. Skill in config but not in registry — silently skipped with warning
# ---------------------------------------------------------------------------


def test_skill_in_config_not_in_registry_logs_warning(caplog):
    """A skill_id present in overrides but missing from the registry should be skipped."""
    registry = SkillRegistry()  # empty
    overrides = {"nonexistent_skill": SkillOverride(enabled=True)}

    with caplog.at_level(logging.WARNING, logger="creel.tools"):
        defs = build_tool_definitions(registry, overrides)

    assert defs == []
    assert "nonexistent_skill" in caplog.text
    assert "not registered" in caplog.text


# ---------------------------------------------------------------------------
# 3. Per-tool secret overrides
# ---------------------------------------------------------------------------


@patch("creel.tools._run_executor_inline_skill")
def test_per_tool_secret_override(mock_run):
    """Per-tool secrets in SkillToolOverride should override skill-level secrets."""
    mock_run.return_value = '{"ok": true}'

    meta = _make_simple_meta("docs", "create_doc")
    registry = _make_registry_with(meta)

    override = SkillOverride(
        enabled=True,
        secrets="secrets/default.enc",
        tools={"create_doc": SkillToolOverride(secrets="secrets/write.enc")},
    )
    skill_overrides = {"docs": override}

    tool_spec, entry = registry.get_tool("create_doc")
    _execute_skill_tool(
        tool_name="create_doc",
        tool_input={"query": "hello"},
        skill_result=(tool_spec, entry),
        skill_overrides=skill_overrides,
        use_containers=False,
        bridge_config=None,
        session_state=None,
        container_pool=None,
    )

    executor_config = mock_run.call_args[0][2]
    assert executor_config.secrets == "secrets/write.enc"


@patch("creel.tools._run_executor_inline_skill")
def test_per_tool_secret_falls_back_to_skill_level(mock_run):
    """When no per-tool override exists, the skill-level secret is used."""
    mock_run.return_value = '{"ok": true}'

    meta = _make_simple_meta("docs", "create_doc")
    registry = _make_registry_with(meta)

    override = SkillOverride(enabled=True, secrets="secrets/default.enc")
    skill_overrides = {"docs": override}

    tool_spec, entry = registry.get_tool("create_doc")
    _execute_skill_tool(
        tool_name="create_doc",
        tool_input={"query": "hello"},
        skill_result=(tool_spec, entry),
        skill_overrides=skill_overrides,
        use_containers=False,
        bridge_config=None,
        session_state=None,
        container_pool=None,
    )

    executor_config = mock_run.call_args[0][2]
    assert executor_config.secrets == "secrets/default.enc"


# ---------------------------------------------------------------------------
# 4. Execution gating — skill not in overrides or disabled
# ---------------------------------------------------------------------------


def test_execute_tool_skill_not_in_overrides():
    """Calling a tool whose skill is absent from skill_overrides should raise."""
    registry = _make_registry()
    # weather tool exists in the full registry, but we pass empty overrides
    with pytest.raises(ValueError, match="not enabled"):
        execute_tool_call("check_weather", {"location": "London"}, registry, {})


def test_execute_tool_skill_disabled():
    """Calling a tool whose skill is explicitly disabled should raise."""
    registry = _make_registry()
    overrides = {"weather": SkillOverride(enabled=False)}
    with pytest.raises(ValueError, match="disabled"):
        execute_tool_call("check_weather", {"location": "London"}, registry, overrides)


# ---------------------------------------------------------------------------
# 5. _build_tool_config_from_skill
# ---------------------------------------------------------------------------


def test_build_tool_config_defaults():
    """Default values when SkillOverride has no explicit resource overrides."""
    meta = _make_simple_meta("my_skill", "my_tool", needs_network=True)
    override = SkillOverride(enabled=True)
    tool_spec = meta.tools[0]

    tc = _build_tool_config_from_skill(meta, override, tool_spec)

    assert tc.executor == "my_skill"
    assert tc.description == tool_spec.description
    assert tc.writable is False
    assert tc.memory == "256m"
    assert tc.cpus == "0.5"
    assert tc.tmpfs_size == "16M"
    assert tc.timeout == 60
    assert tc.network is True  # fallback to meta.needs_network
    assert tc.secrets is None
    assert tc.host_auth is False
    assert tc.mounts == []
    assert tc.classify_output is False
    assert tc.cache_ttl == 0


def test_build_tool_config_override_values():
    """Explicit SkillOverride values should take precedence."""
    meta = _make_simple_meta("my_skill", "my_tool", needs_network=False)
    override = SkillOverride(
        enabled=True,
        secrets="secrets/test.enc",
        writable=True,
        memory="512m",
        cpus="2.0",
        tmpfs_size="64M",
        timeout=120,
        network=True,
        host_auth=True,
        classify_output=True,
        cache_ttl=300,
        http=HttpConfig(timeout=30.0),
    )
    tool_spec = meta.tools[0]

    tc = _build_tool_config_from_skill(meta, override, tool_spec)

    assert tc.executor == "my_skill"
    assert tc.writable is True
    assert tc.memory == "512m"
    assert tc.cpus == "2.0"
    assert tc.tmpfs_size == "64M"
    assert tc.timeout == 120
    assert tc.network is True  # explicitly set, not from meta
    assert tc.secrets == "secrets/test.enc"
    assert tc.host_auth is True
    assert tc.classify_output is True
    assert tc.cache_ttl == 300
    assert tc.http.timeout == 30.0


def test_build_tool_config_network_fallback():
    """When override.network is None, falls back to meta.needs_network."""
    meta_net = _make_simple_meta("net_skill", "net_tool", needs_network=True)
    meta_no_net = _make_simple_meta("nonet_skill", "nonet_tool", needs_network=False)
    override = SkillOverride(enabled=True)  # network=None

    tc_net = _build_tool_config_from_skill(meta_net, override, meta_net.tools[0])
    tc_nonet = _build_tool_config_from_skill(meta_no_net, override, meta_no_net.tools[0])

    assert tc_net.network is True
    assert tc_nonet.network is False
