"""Tests for the system prompt builder."""

import tempfile
from pathlib import Path

from taskrunner.prompt_builder import build_system_prompt, _build_workspace_section


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
