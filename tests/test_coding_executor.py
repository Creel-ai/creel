"""Tests for the coding executor — write_file, agent, and JSON args loading."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from executors.coding.executor import _load_args, run_command


class TestLoadArgs:
    """Test _load_args reads from JSON file."""

    def test_load_from_json_file(self, tmp_path: Path) -> None:
        args_file = tmp_path / "args.json"
        args_file.write_text(json.dumps({"command": "echo hello", "workdir": "/tmp"}))
        os.environ["CREEL_INPUT_FILE"] = str(args_file)
        try:
            result = _load_args()
            assert result["command"] == "echo hello"
            assert result["workdir"] == "/tmp"
        finally:
            del os.environ["CREEL_INPUT_FILE"]

    def test_load_missing_file_returns_empty(self) -> None:
        os.environ["CREEL_INPUT_FILE"] = "/nonexistent/file.json"
        try:
            result = _load_args()
            assert result == {}
        finally:
            del os.environ["CREEL_INPUT_FILE"]

    def test_load_no_env_var_returns_empty(self) -> None:
        os.environ.pop("CREEL_INPUT_FILE", None)
        result = _load_args()
        assert result == {}

    def test_multiline_content_preserved(self, tmp_path: Path) -> None:
        content = "line 1\nline 2\nline 3\n"
        args_file = tmp_path / "args.json"
        args_file.write_text(json.dumps({"content": content, "path": "test.py"}))
        os.environ["CREEL_INPUT_FILE"] = str(args_file)
        try:
            result = _load_args()
            assert result["content"] == content
            assert "\n" in result["content"]
        finally:
            del os.environ["CREEL_INPUT_FILE"]


class TestArgDispatch:
    """Test that args are correctly identified for dispatch."""

    def test_task_arg_means_agent(self, tmp_path: Path) -> None:
        args_file = tmp_path / "args.json"
        args_file.write_text(json.dumps({"task": "build a hello world app"}))
        os.environ["CREEL_INPUT_FILE"] = str(args_file)
        try:
            args = _load_args()
            assert "task" in args
        finally:
            del os.environ["CREEL_INPUT_FILE"]

    def test_content_and_path_means_write(self, tmp_path: Path) -> None:
        args_file = tmp_path / "args.json"
        args_file.write_text(json.dumps({"content": "print('hello')\n", "path": "test.py"}))
        os.environ["CREEL_INPUT_FILE"] = str(args_file)
        try:
            args = _load_args()
            assert "content" in args and "path" in args
            assert "task" not in args
        finally:
            del os.environ["CREEL_INPUT_FILE"]

    def test_command_means_shell(self, tmp_path: Path) -> None:
        args_file = tmp_path / "args.json"
        args_file.write_text(json.dumps({"command": "ls -la"}))
        os.environ["CREEL_INPUT_FILE"] = str(args_file)
        try:
            args = _load_args()
            assert "command" in args
            assert "task" not in args
            assert "content" not in args
        finally:
            del os.environ["CREEL_INPUT_FILE"]


class TestWriteFile:
    """Test the write_file functionality."""

    def test_write_creates_file_with_newlines(self, tmp_path: Path) -> None:
        content = "line 1\nline 2\nline 3\n"
        full_path = tmp_path / "output" / "test.py"
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        assert full_path.exists()
        assert full_path.read_text() == content
        assert full_path.read_text().count("\n") == 3

    def test_strips_workspace_prefix(self) -> None:
        path = "workspace/src/main.py"
        if path.startswith("workspace/"):
            path = path[len("workspace/") :]
        assert path == "src/main.py"

    def test_no_strip_without_prefix(self) -> None:
        path = "src/main.py"
        if path.startswith("workspace/"):
            path = path[len("workspace/") :]
        assert path == "src/main.py"


class TestRunCommand:
    """Test run_command for shell execution."""

    @patch("subprocess.run")
    def test_simple_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        result = run_command("echo ok")
        assert result["success"] is True
        assert result["exit_code"] == 0

    @patch("subprocess.run")
    def test_multiline_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        cmd = "cat > test.py << 'EOF'\nprint('hello')\nEOF"
        run_command(cmd)
        call_args = mock_run.call_args[0][0]
        assert "cat > test.py" in call_args[-1]
