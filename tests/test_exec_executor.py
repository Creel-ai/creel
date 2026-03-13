"""Tests for the exec executor."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from creel.models import ExecutorConfig, MountConfig, ToolConfig
from creel.orchestrator import _run_executor_container
from executors.exec.executor import run_command


class TestExecExecutor:
    """Tests for the exec executor function directly."""

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

    def test_command_with_workdir(self) -> None:
        """Test executing a command with a working directory."""
        with (
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="test output",
                stderr="",
            )

            result = run_command("pwd", workdir="/tmp")

            assert result["success"] is True
            assert result["workdir"] == "/tmp"

            mock_run.assert_called_once_with(
                ["bash", "-c", "pwd"],
                cwd="/tmp",
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
                stderr="command not found",
            )

            result = run_command("nonexistent_command")

            assert result["success"] is False
            assert result["exit_code"] == 1
            assert result["stderr"] == "command not found"

    def test_command_timeout(self) -> None:
        """Test command that times out."""
        import subprocess

        with patch("subprocess.run") as mock_run:
            timeout_exception = subprocess.TimeoutExpired(
                cmd=["bash", "-c", "sleep 10"],
                timeout=300,
            )
            timeout_exception.stdout = b"partial output"
            timeout_exception.stderr = b""
            mock_run.side_effect = timeout_exception

            result = run_command("sleep 10")

            assert result["success"] is False
            assert result["exit_code"] == -1
            assert "timed out" in result["error"]
            assert result["stdout"] == "partial output"

    def test_workdir_validation(self) -> None:
        """Test working directory validation."""
        # Test nonexistent directory
        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(FileNotFoundError, match="does not exist"):
                run_command("echo test", workdir="/nonexistent")

        # Test file instead of directory
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_dir", return_value=False),
        ):
            with pytest.raises(NotADirectoryError, match="not a directory"):
                run_command("echo test", workdir="/etc/passwd")


class TestMountConfig:
    """Tests for MountConfig model."""

    def test_mount_config_defaults(self) -> None:
        """Test MountConfig with default values."""
        mount = MountConfig(path="/home/user")
        assert mount.path == "/home/user"
        assert mount.mode == "ro"

    def test_mount_config_explicit_mode(self) -> None:
        """Test MountConfig with explicit mode."""
        mount = MountConfig(path="/tmp", mode="rw")
        assert mount.path == "/tmp"
        assert mount.mode == "rw"

    def test_mount_config_invalid_mode(self) -> None:
        """Test MountConfig with invalid mode."""
        with pytest.raises(ValueError, match="String should match pattern"):
            MountConfig(path="/tmp", mode="invalid")


class TestToolConfigExtensions:
    """Tests for new ToolConfig fields."""

    def test_tool_config_defaults(self) -> None:
        """Test ToolConfig with new default values."""
        config = ToolConfig(executor="exec", description="Test tool")
        assert config.mounts == []
        assert config.network is False
        assert config.image is None

    def test_tool_config_with_mounts(self) -> None:
        """Test ToolConfig with mount configuration."""
        mount = MountConfig(path="/workspace", mode="rw")
        config = ToolConfig(
            executor="exec",
            description="Test tool",
            mounts=[mount],
            network=True,
            image="custom:latest",
        )
        assert len(config.mounts) == 1
        assert config.mounts[0].path == "/workspace"
        assert config.mounts[0].mode == "rw"
        assert config.network is True
        assert config.image == "custom:latest"


class TestContainerExecution:
    """Tests for container execution with mount and network options."""

    @patch("creel.containers._ensure_image")
    @patch("subprocess.run")
    @patch("tempfile.NamedTemporaryFile")
    def test_container_with_mounts(self, mock_tempfile, mock_subprocess, mock_ensure_image) -> None:
        """Test container execution with mount configuration."""
        # Mock tempfile
        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tempfile.return_value.__enter__.return_value = mock_env_file

        # Mock subprocess result
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout='{"result": "success"}',
            stderr="",
        )

        # Create test configs
        executor_config = ExecutorConfig(name="exec")
        mount = MountConfig(path="~/workspace", mode="rw")
        tool_config = ToolConfig(executor="exec", description="Test", mounts=[mount], network=False)

        with patch("os.path.expanduser", return_value="/home/user/workspace"):
            _run_executor_container(executor_config, tool_config)

        # Check that docker run was called with mount options
        mock_subprocess.assert_called_once()
        docker_cmd = mock_subprocess.call_args[0][0]

        assert "docker" in docker_cmd
        assert "--network=none" in docker_cmd
        assert "-v" in docker_cmd

        # Find the mount argument
        mount_idx = docker_cmd.index("-v")
        mount_arg = docker_cmd[mount_idx + 1]
        assert "/home/user/workspace:/mnt/home/user/workspace:rw" == mount_arg

    @patch("creel.containers._ensure_image")
    @patch("subprocess.run")
    @patch("tempfile.NamedTemporaryFile")
    def test_container_with_network_enabled(
        self, mock_tempfile, mock_subprocess, mock_ensure_image
    ) -> None:
        """Test container execution with network enabled."""
        # Mock tempfile
        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tempfile.return_value.__enter__.return_value = mock_env_file

        # Mock subprocess result
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout='{"result": "success"}',
            stderr="",
        )

        # Create test configs
        executor_config = ExecutorConfig(name="exec")
        tool_config = ToolConfig(
            executor="exec",
            description="Test",
            network=True,  # Network enabled
        )

        _run_executor_container(executor_config, tool_config)

        # Check that docker run was called without --network=none
        docker_cmd = mock_subprocess.call_args[0][0]
        assert "--network=none" not in docker_cmd

    @patch("creel.containers._ensure_image")
    @patch("subprocess.run")
    @patch("tempfile.NamedTemporaryFile")
    def test_container_with_image_override(
        self, mock_tempfile, mock_subprocess, mock_ensure_image
    ) -> None:
        """Test container execution with image override."""
        # Mock tempfile
        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tempfile.return_value.__enter__.return_value = mock_env_file

        # Mock _ensure_image to return the image name (content-hash passthrough)
        mock_ensure_image.return_value = "custom-exec:v1.0"

        # Mock subprocess result
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout='{"result": "success"}',
            stderr="",
        )

        # Create test configs
        executor_config = ExecutorConfig(name="exec")
        tool_config = ToolConfig(
            executor="exec",
            description="Test",
            image="custom-exec:v1.0",  # Image override
        )

        _run_executor_container(executor_config, tool_config)

        # Check that the custom image was used
        mock_ensure_image.assert_called_with("custom-exec:v1.0")

        docker_cmd = mock_subprocess.call_args[0][0]
        assert "custom-exec:v1.0" in docker_cmd


class TestPathExpansion:
    """Tests for mount path expansion."""

    def test_home_directory_expansion(self) -> None:
        """Test that ~ expands to home directory in mount paths."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/home/testuser/workspace"

            mount = MountConfig(path="~/workspace")

            # This would be tested in the container execution logic
            expanded_path = os.path.expanduser(mount.path)
            assert expanded_path == "/home/testuser/workspace"
            mock_expand.assert_called_once_with("~/workspace")


class TestToolConfigContainerOverrides:
    """Tests for per-executor container resource override fields on ToolConfig."""

    def test_defaults(self) -> None:
        """Test that new container fields have sensible defaults."""
        config = ToolConfig(executor="exec", description="Test tool")
        assert config.writable is False
        assert config.tmpfs_size == "16M"
        assert config.memory == "256m"
        assert config.cpus == "0.5"
        assert config.timeout == 60

    def test_custom_values(self) -> None:
        """Test setting custom container resource values."""
        config = ToolConfig(
            executor="coding",
            description="Dev env",
            writable=True,
            tmpfs_size="256M",
            memory="512m",
            cpus="1.0",
            timeout=300,
        )
        assert config.writable is True
        assert config.tmpfs_size == "256M"
        assert config.memory == "512m"
        assert config.cpus == "1.0"
        assert config.timeout == 300

    def test_tmpfs_size_validates_docker_format(self) -> None:
        """Test that tmpfs_size rejects invalid formats."""
        with pytest.raises(ValueError, match="tmpfs_size must be a Docker size"):
            ToolConfig(executor="exec", description="t", tmpfs_size="invalid")

    def test_tmpfs_size_allows_valid_formats(self) -> None:
        """Test valid Docker size formats for tmpfs_size."""
        for size in ("16M", "256M", "1G", "512k"):
            config = ToolConfig(executor="exec", description="t", tmpfs_size=size)
            assert config.tmpfs_size == size

    def test_memory_validates_docker_format(self) -> None:
        """Test that memory rejects invalid formats."""
        with pytest.raises(ValueError, match="memory must be a Docker size"):
            ToolConfig(executor="exec", description="t", memory="oops")

    def test_cpus_validates_positive_float(self) -> None:
        """Test that cpus rejects non-numeric and non-positive values."""
        with pytest.raises(ValueError, match="cpus must be a positive number"):
            ToolConfig(executor="exec", description="t", cpus="abc")
        with pytest.raises(ValueError, match="cpus must be positive"):
            ToolConfig(executor="exec", description="t", cpus="0")
        with pytest.raises(ValueError, match="cpus must be positive"):
            ToolConfig(executor="exec", description="t", cpus="-1")

    def test_timeout_must_be_positive(self) -> None:
        """Test that timeout must be >= 1."""
        with pytest.raises(ValueError):
            ToolConfig(executor="exec", description="t", timeout=0)


class TestContainerResourceOverrides:
    """Tests for per-executor resource overrides in _run_executor_container."""

    @patch("creel.containers._ensure_image")
    @patch("subprocess.run")
    @patch("tempfile.NamedTemporaryFile")
    def test_writable_omits_read_only(self, mock_tempfile, mock_subprocess, mock_ensure_image):
        """Test that writable=True omits --read-only from docker command."""
        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tempfile.return_value.__enter__.return_value = mock_env_file
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="{}", stderr="")

        executor_config = ExecutorConfig(name="coding")
        tool_config = ToolConfig(
            executor="coding",
            description="Dev",
            writable=True,
            network=True,
        )

        _run_executor_container(executor_config, tool_config)

        docker_cmd = mock_subprocess.call_args[0][0]
        assert "--read-only" not in docker_cmd

    @patch("creel.containers._ensure_image")
    @patch("subprocess.run")
    @patch("tempfile.NamedTemporaryFile")
    def test_readonly_default(self, mock_tempfile, mock_subprocess, mock_ensure_image):
        """Test that writable=False (default) includes --read-only."""
        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tempfile.return_value.__enter__.return_value = mock_env_file
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="{}", stderr="")

        executor_config = ExecutorConfig(name="weather")
        tool_config = ToolConfig(executor="weather", description="Weather")

        _run_executor_container(executor_config, tool_config)

        docker_cmd = mock_subprocess.call_args[0][0]
        assert "--read-only" in docker_cmd

    @patch("creel.containers._ensure_image")
    @patch("subprocess.run")
    @patch("tempfile.NamedTemporaryFile")
    def test_custom_memory_cpus_tmpfs(self, mock_tempfile, mock_subprocess, mock_ensure_image):
        """Test custom memory, cpus, and tmpfs_size are passed to docker."""
        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tempfile.return_value.__enter__.return_value = mock_env_file
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="{}", stderr="")

        executor_config = ExecutorConfig(name="coding")
        tool_config = ToolConfig(
            executor="coding",
            description="Dev",
            memory="512m",
            cpus="2.0",
            tmpfs_size="256M",
            network=True,
        )

        _run_executor_container(executor_config, tool_config)

        docker_cmd = mock_subprocess.call_args[0][0]
        assert "--memory=512m" in docker_cmd
        assert "--cpus=2.0" in docker_cmd
        # Check tmpfs contains the custom size
        tmpfs_idx = docker_cmd.index("--tmpfs")
        assert "size=256M" in docker_cmd[tmpfs_idx + 1]

    @patch("creel.containers._ensure_image")
    @patch("subprocess.run")
    @patch("tempfile.NamedTemporaryFile")
    def test_cap_drop_always_present(self, mock_tempfile, mock_subprocess, mock_ensure_image):
        """Test that --cap-drop=ALL is always present regardless of writable."""
        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tempfile.return_value.__enter__.return_value = mock_env_file
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="{}", stderr="")

        executor_config = ExecutorConfig(name="coding")
        tool_config = ToolConfig(
            executor="coding",
            description="Dev",
            writable=True,
            network=True,
        )

        _run_executor_container(executor_config, tool_config)

        docker_cmd = mock_subprocess.call_args[0][0]
        assert "--cap-drop=ALL" in docker_cmd
        assert "--security-opt=no-new-privileges" in docker_cmd

    @patch("creel.containers._ensure_image")
    @patch("subprocess.run")
    @patch("tempfile.NamedTemporaryFile")
    def test_timeout_threaded_from_tool_config(
        self, mock_tempfile, mock_subprocess, mock_ensure_image
    ):
        """Test that timeout from ToolConfig flows through to ExecutorConfig."""
        from creel.tools import execute_tool_call

        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tempfile.return_value.__enter__.return_value = mock_env_file
        mock_subprocess.return_value = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")

        tools_config = {
            "coding": ToolConfig(
                executor="coding",
                description="Dev",
                timeout=300,
                network=True,
            ),
        }

        with patch("creel.tools._run_executor_container") as mock_container:
            mock_container.return_value = '{"ok": true}'
            execute_tool_call("coding", {"command": "echo hi"}, tools_config, use_containers=True)
            exec_config = mock_container.call_args[0][0]
            assert exec_config.timeout == 300
