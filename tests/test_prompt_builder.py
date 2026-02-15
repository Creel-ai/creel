"""Tests for the system prompt builder."""

import tempfile
from pathlib import Path
from unittest.mock import patch

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

    def test_system_prompt_file_loads(self):
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "custom_prompt.md"
            prompt_file.write_text("You are a custom assistant with special powers.")
            
            result = build_system_prompt(
                base_prompt="You are a basic assistant.",
                system_prompt_file=str(prompt_file)
            )
            assert "custom assistant with special powers" in result
            assert "basic assistant" not in result

    def test_system_prompt_file_missing_fallback(self):
        with patch('taskrunner.prompt_builder.logger') as mock_logger:
            result = build_system_prompt(
                base_prompt="You are a fallback assistant.",
                system_prompt_file="/nonexistent/prompt.md"
            )
            assert "fallback assistant" in result
            mock_logger.warning.assert_called_once()
            assert "not found" in mock_logger.warning.call_args[0][0]

    def test_system_prompt_file_read_error(self):
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "unreadable_prompt.md"
            prompt_file.write_text("You are a custom assistant.")
            prompt_file.chmod(0o000)  # Make file unreadable
            
            with patch('taskrunner.prompt_builder.logger') as mock_logger:
                result = build_system_prompt(
                    base_prompt="You are a fallback assistant.",
                    system_prompt_file=str(prompt_file)
                )
                assert "fallback assistant" in result
                mock_logger.warning.assert_called_once()
                assert "Failed to read" in mock_logger.warning.call_args[0][0]

    def test_system_prompt_file_no_base_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "custom_prompt.md"
            prompt_file.write_text("You are a specialized agent.")
            
            result = build_system_prompt(system_prompt_file=str(prompt_file))
            assert "specialized agent" in result
            assert "personal assistant" not in result

    def test_system_prompt_file_with_workspace_context(self):
        with tempfile.TemporaryDirectory() as td:
            # Create system prompt file
            prompt_file = Path(td) / "prompt.md"
            prompt_file.write_text("You are a project assistant.")
            
            # Create workspace files
            (Path(td) / "SOUL.md").write_text("# Soul\nI am focused on productivity.")
            
            result = build_system_prompt(
                system_prompt_file=str(prompt_file),
                workspace_dir=td
            )
            assert "project assistant" in result
            assert "focused on productivity" in result
            assert "Workspace Context" in result


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
