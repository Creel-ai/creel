"""Tests for the system prompt builder."""

import tempfile
from pathlib import Path

from creel.prompt_builder import (
    _build_tool_guidance_from_registry,
    _build_workspace_section,
    build_system_prompt,
)
from creel.skills.models import SkillMeta, ToolSpec
from creel.skills.registry import SkillRegistry


class TestBuildSystemPrompt:
    def test_default_prompt(self):
        result = build_system_prompt()
        assert "personal assistant" in result
        assert "Current Date & Time" in result

    def test_custom_base_prompt(self):
        result = build_system_prompt(base_prompt="You are a pirate.")
        assert "You are a pirate." in result
        assert "personal assistant" not in result

    def test_timezone_injection(self):
        result = build_system_prompt(timezone_name="America/Denver")
        assert "America/Denver" in result

    def test_invalid_timezone_falls_back(self):
        result = build_system_prompt(timezone_name="Invalid/Zone")
        assert "Current Date & Time" in result

    def test_workspace_injection(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "SOUL.md").write_text("# Soul\nI am helpful.")
            (Path(td) / "USER.md").write_text("# User\nName: Test")
            result = build_system_prompt(workspace_dir=td)
            assert "I am helpful" in result
            assert "Name: Test" in result

    def test_workspace_missing_files_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "SOUL.md").write_text("# Soul\nHello")
            result = build_system_prompt(workspace_dir=td)
            assert "Hello" in result
            # USER.md doesn't exist, shouldn't cause error
            assert "USER.md" not in result

    def test_workspace_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "SOUL.md").write_text("x" * 30_000)
            result = build_system_prompt(workspace_dir=td, max_chars_per_file=1000)
            assert "[... truncated]" in result

    def test_memory_context_injection(self):
        result = build_system_prompt(memory_context="User prefers dark mode.")
        assert "Relevant Memory" in result
        assert "dark mode" in result

    def test_nonexistent_workspace(self):
        result = build_system_prompt(workspace_dir="/nonexistent/path")
        # Should not crash, workspace section just omitted
        assert "Workspace Context" not in result


class TestBuildWorkspaceSection:
    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            result = _build_workspace_section(td, 20_000)
            assert result is None

    def test_empty_files_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "SOUL.md").write_text("")
            result = _build_workspace_section(td, 20_000)
            assert result is None


# -- Helpers for registry tests --


def _make_registry(*skills: SkillMeta) -> SkillRegistry:
    """Build a SkillRegistry pre-loaded with the given SkillMeta entries."""
    registry = SkillRegistry()
    for skill in skills:
        registry.register(skill, execute=lambda cfg: "")
    return registry


def _weather_skill() -> SkillMeta:
    return SkillMeta(
        id="weather",
        label="Weather",
        tools=(ToolSpec(name="get_weather", description="Get the current weather for a location"),),
    )


def _gmail_skill() -> SkillMeta:
    return SkillMeta(
        id="gmail_readonly",
        label="Gmail (read-only)",
        tools=(
            ToolSpec(name="search_email", description="Search emails by query"),
            ToolSpec(name="read_email", description="Read a single email by ID"),
        ),
    )


# -- _build_tool_guidance_from_registry tests --


class TestBuildToolGuidanceFromRegistry:
    def test_empty_registry_returns_none(self):
        """An empty registry should produce no tool guidance section."""
        registry = SkillRegistry()
        result = _build_tool_guidance_from_registry(registry)
        assert result is None

    def test_populated_registry_returns_markdown(self):
        """A registry with skills should return markdown listing each tool."""
        registry = _make_registry(_weather_skill(), _gmail_skill())
        result = _build_tool_guidance_from_registry(registry)

        assert result is not None
        assert result.startswith("## Available Tools")
        # Weather tool
        assert "- **get_weather**: Get the current weather for a location" in result
        # Gmail tools
        assert "- **search_email**: Search emails by query" in result
        assert "- **read_email**: Read a single email by ID" in result

    def test_multi_tool_skill_lists_all_tools(self):
        """A skill providing multiple tools should have each tool listed."""
        registry = _make_registry(_gmail_skill())
        result = _build_tool_guidance_from_registry(registry)

        assert result is not None
        lines = result.strip().splitlines()
        # Header + 2 tool lines
        assert len(lines) == 3
        assert lines[0] == "## Available Tools"


# -- build_system_prompt registry integration tests --


class TestBuildSystemPromptRegistry:
    def test_with_registry_includes_tool_guidance(self):
        """build_system_prompt should include tool guidance when a registry is provided."""
        registry = _make_registry(_weather_skill())
        result = build_system_prompt(registry=registry)

        assert "## Available Tools" in result
        assert "**get_weather**" in result

    def test_without_registry_omits_tool_guidance(self):
        """build_system_prompt should NOT include tool guidance when registry is None."""
        result = build_system_prompt(registry=None)

        assert "## Available Tools" not in result
        assert "get_weather" not in result
