"""Tests for the host_auth feature on ToolConfig / orchestrator."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from creel.models import ExecutorConfig, ToolConfig
from creel.orchestrator import _run_executor_container

# ---------------------------------------------------------------------------
# Model-level tests
# ---------------------------------------------------------------------------


class TestToolConfigHostAuth:
    """ToolConfig.host_auth field basics."""

    def test_defaults_to_false(self) -> None:
        tc = ToolConfig(executor="github", description="test")
        assert tc.host_auth is False

    def test_accepts_true(self) -> None:
        tc = ToolConfig(executor="github", description="test", host_auth=True)
        assert tc.host_auth is True

    def test_host_auth_without_secrets(self) -> None:
        """host_auth=True with secrets=None should be a valid config."""
        tc = ToolConfig(executor="github", description="test", host_auth=True, secrets=None)
        assert tc.host_auth is True
        assert tc.secrets is None


# ---------------------------------------------------------------------------
# Orchestrator-level tests
# ---------------------------------------------------------------------------


class TestHostAuthContainerMount:
    """Verify _run_executor_container handles host_auth correctly."""

    def test_unsupported_executor_raises_value_error(self) -> None:
        """host_auth=True on an executor not in the registry → ValueError."""
        config = ExecutorConfig(name="weather")
        tool_config = ToolConfig(executor="weather", description="test", host_auth=True)

        with pytest.raises(ValueError, match="host_auth is not supported for executor 'weather'"):
            _run_executor_container(config, tool_config=tool_config)

    def test_missing_auth_dir_raises_runtime_error(self, tmp_path: Path) -> None:
        """host_auth=True + auth dir doesn't exist → RuntimeError."""
        config = ExecutorConfig(name="github")
        tool_config = ToolConfig(executor="github", description="test", host_auth=True)

        fake_path = str(tmp_path / "nonexistent")
        patched_registry = {
            "github": {
                "host_path": fake_path,
                "container_path": "/home/executor/.config/gh",
            },
        }

        with (
            patch.dict("creel.containers._HOST_AUTH_REGISTRY", patched_registry, clear=True),
            patch("creel.containers._ensure_image", return_value="executor-github:latest"),
            patch("creel.containers.decrypt_env_file", return_value={}),
            pytest.raises(RuntimeError, match="Host auth directory not found"),
        ):
            _run_executor_container(config, tool_config=tool_config)

    def test_valid_host_auth_adds_volume_flag(self, tmp_path: Path) -> None:
        """host_auth=True + existing dir → correct -v flag in docker command."""
        config = ExecutorConfig(name="github")
        tool_config = ToolConfig(executor="github", description="test", host_auth=True)

        # Create a fake auth directory
        auth_dir = tmp_path / ".config" / "gh"
        auth_dir.mkdir(parents=True)

        patched_registry = {
            "github": {
                "host_path": str(auth_dir),
                "container_path": "/home/executor/.config/gh",
            },
        }

        with (
            patch.dict("creel.containers._HOST_AUTH_REGISTRY", patched_registry, clear=True),
            patch("creel.containers._ensure_image", return_value="executor-github:latest"),
            patch("creel.containers.decrypt_env_file", return_value={}),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = SimpleNamespace(
                returncode=0,
                stdout='{"ok": true}',
                stderr="",
            )

            _run_executor_container(config, tool_config=tool_config)

            # Extract the docker command from the subprocess.run call
            docker_cmd = mock_run.call_args[0][0]
            expected_mount = f"{auth_dir}:/home/executor/.config/gh:ro"
            assert "-v" in docker_cmd
            # Find the -v flag that corresponds to our host_auth mount
            found = False
            for i, arg in enumerate(docker_cmd):
                if arg == "-v" and i + 1 < len(docker_cmd) and docker_cmd[i + 1] == expected_mount:
                    found = True
                    break
            assert found, (
                f"Expected volume mount '{expected_mount}' not found in docker command: {docker_cmd}"
            )

    def test_host_auth_false_no_mount(self) -> None:
        """host_auth=False (default) should not add any host-auth volume mounts."""
        config = ExecutorConfig(name="github")
        tool_config = ToolConfig(executor="github", description="test")

        with (
            patch("creel.containers._ensure_image", return_value="executor-github:latest"),
            patch("creel.containers.decrypt_env_file", return_value={}),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = SimpleNamespace(
                returncode=0,
                stdout='{"ok": true}',
                stderr="",
            )

            _run_executor_container(config, tool_config=tool_config)

            docker_cmd = mock_run.call_args[0][0]
            # No -v flag should reference the gh config path
            for i, arg in enumerate(docker_cmd):
                if arg == "-v" and i + 1 < len(docker_cmd):
                    assert ".config/gh" not in docker_cmd[i + 1], (
                        "host_auth=False should not mount gh config"
                    )

    def test_host_auth_with_secrets_raises_value_error(self) -> None:
        """host_auth=True + secrets set → ValueError (mutually exclusive)."""
        config = ExecutorConfig(name="github")
        tool_config = ToolConfig(
            executor="github",
            description="test",
            host_auth=True,
            secrets="secrets/github.env.enc",
        )

        with pytest.raises(ValueError, match="host_auth and secrets are mutually exclusive"):
            _run_executor_container(config, tool_config=tool_config)
