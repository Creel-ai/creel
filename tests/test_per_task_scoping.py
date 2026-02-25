"""Tests for per-task tool scoping."""

from __future__ import annotations

from pathlib import Path

import pytest

from creel.models import TaskDefinition


class TestTaskDefinitionAllowedTools:
    """Tests for the allowed_tools field on TaskDefinition."""

    def test_default_empty(self) -> None:
        """allowed_tools should default to empty list."""
        task = TaskDefinition(
            name="test",
            schedule="0 7 * * *",
            prompt="test",
            output={"type": "stdout", "to": "-"},
        )
        assert task.allowed_tools == []

    def test_allowed_tools_set(self) -> None:
        """allowed_tools should accept a list of tool names."""
        task = TaskDefinition(
            name="test",
            schedule="0 7 * * *",
            prompt="test",
            output={"type": "stdout", "to": "-"},
            allowed_tools=["check_weather", "check_calendar", "check_email"],
        )
        assert task.allowed_tools == ["check_weather", "check_calendar", "check_email"]

    def test_load_task_with_allowed_tools(self, tmp_path: Path) -> None:
        """Loading a task YAML with allowed_tools should populate the field."""
        task_file = tmp_path / "test_task.yaml"
        task_file.write_text("""\
name: test_task
schedule: "0 7 * * *"
prompt: "Test prompt"
output:
  type: stdout
  to: "-"
allowed_tools:
  - check_weather
  - check_calendar
""")
        from creel.models import load_task
        task = load_task(task_file)
        assert task.allowed_tools == ["check_weather", "check_calendar"]

    def test_load_task_without_allowed_tools(self, tmp_path: Path) -> None:
        """Loading a task YAML without allowed_tools should default to empty."""
        task_file = tmp_path / "test_task.yaml"
        task_file.write_text("""\
name: test_task
schedule: "0 7 * * *"
prompt: "Test prompt"
output:
  type: stdout
  to: "-"
""")
        from creel.models import load_task
        task = load_task(task_file)
        assert task.allowed_tools == []
