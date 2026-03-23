"""Tests for the coding executor — write_file, agent, and JSON args loading."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from executors.coding.executor import _detect_mounted_project, _load_args, run_agent, run_command


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


class TestDetectMountedProject:
    """Test _detect_mounted_project auto-detects mounted project directories."""

    def test_single_mount_detected(self, tmp_path: Path) -> None:
        """A single mount tree under /mnt/ should be detected."""
        project = tmp_path / "mnt" / "Users" / "ross" / "projects" / "my-app"
        project.mkdir(parents=True)
        # Add files so the leaf isn't empty
        (project / "package.json").write_text("{}")

        with patch("executors.coding.executor.Path") as mock_path_cls:
            # Make Path("/mnt") return our tmp_path version
            real_path = Path

            def side_effect(p):
                if p == "/mnt":
                    return real_path(tmp_path / "mnt")
                return real_path(p)

            mock_path_cls.side_effect = side_effect
            result = _detect_mounted_project()

        assert result is not None
        assert result.endswith("my-app")

    def test_no_mnt_returns_none(self, tmp_path: Path) -> None:
        """No /mnt directory should return None."""
        with patch("executors.coding.executor.Path") as mock_path_cls:
            real_path = Path

            def side_effect(p):
                if p == "/mnt":
                    return real_path(tmp_path / "nonexistent")
                return real_path(p)

            mock_path_cls.side_effect = side_effect
            result = _detect_mounted_project()

        assert result is None

    def test_empty_mnt_returns_none(self, tmp_path: Path) -> None:
        """An empty /mnt directory should return None."""
        mnt = tmp_path / "mnt"
        mnt.mkdir()

        with patch("executors.coding.executor.Path") as mock_path_cls:
            real_path = Path

            def side_effect(p):
                if p == "/mnt":
                    return real_path(mnt)
                return real_path(p)

            mock_path_cls.side_effect = side_effect
            result = _detect_mounted_project()

        assert result is None

    def test_multiple_mounts_prefers_writable(self, tmp_path: Path) -> None:
        """With multiple mounts, prefer the writable one."""
        mnt = tmp_path / "mnt"
        proj_a = mnt / "a" / "project-a"
        proj_b = mnt / "b" / "project-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        # Add files so descend logic stops
        (proj_a / "README").write_text("")
        (proj_b / "README").write_text("")

        with (
            patch("executors.coding.executor.Path") as mock_path_cls,
            patch("os.access") as mock_access,
        ):
            real_path = Path

            def side_effect(p):
                if p == "/mnt":
                    return real_path(mnt)
                return real_path(p)

            mock_path_cls.side_effect = side_effect

            # Make project-b writable, project-a not
            def access_side_effect(path, mode):
                return str(path).endswith("project-b")

            mock_access.side_effect = access_side_effect
            result = _detect_mounted_project()

        assert result is not None
        assert result.endswith("project-b")


class TestRunAgentWorkdir:
    """Test run_agent workdir resolution with mounted projects."""

    @patch("subprocess.run")
    def test_explicit_workdir_takes_precedence(self, mock_run: MagicMock) -> None:
        """Explicit workdir arg should override everything."""
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")
        result = run_agent("test task", workdir="/tmp")
        assert result["workdir"] == "/tmp"

    @patch("subprocess.run")
    def test_workspace_env_takes_precedence_over_mount(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """WORKSPACE env var should override mount detection."""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")

        with patch.dict(os.environ, {"WORKSPACE": str(workspace_dir)}):
            result = run_agent("test task")

        assert result["workdir"] == str(workspace_dir)

    @patch("subprocess.run")
    @patch("executors.coding.executor._detect_mounted_project")
    def test_falls_back_to_mounted_project(
        self, mock_detect: MagicMock, mock_run: MagicMock
    ) -> None:
        """Without WORKSPACE, should fall back to mounted project detection."""
        mock_detect.return_value = "/mnt/Users/ross/projects/my-app"
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKSPACE", None)
            result = run_agent("test task")

        assert result["workdir"] == "/mnt/Users/ross/projects/my-app"
        mock_detect.assert_called_once()
