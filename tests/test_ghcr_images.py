"""Tests for GHCR pre-built image support and custom Dockerfile config."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from creel.containers import (
    _ensure_image_from_dockerfile,
    _ensure_image_uncached,
    _is_remote_image,
    _pull_image,
    collect_required_images,
    pull_required_images,
)
from creel.models import AgentDefinition, ToolConfig


class TestIsRemoteImage:
    """Tests for remote image detection."""

    def test_ghcr_image_is_remote(self) -> None:
        assert _is_remote_image("ghcr.io/creel-ai/executor-weather:latest") is True

    def test_docker_hub_image_is_remote(self) -> None:
        assert _is_remote_image("myorg/myimage:v1") is True

    def test_local_image_is_not_remote(self) -> None:
        assert _is_remote_image("executor-weather:latest") is False

    def test_local_image_no_tag(self) -> None:
        assert _is_remote_image("executor-weather") is False

    def test_deeply_nested_registry(self) -> None:
        assert _is_remote_image("registry.example.com/org/repo:tag") is True


class TestPullImage:
    """Tests for _pull_image."""

    @patch("subprocess.run")
    def test_pull_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = _pull_image("ghcr.io/creel-ai/executor-weather:latest")
        assert result == "ghcr.io/creel-ai/executor-weather:latest"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "pull", "ghcr.io/creel-ai/executor-weather:latest"]

    @patch("subprocess.run")
    def test_pull_failure_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")

        with pytest.raises(RuntimeError, match="Failed to pull"):
            _pull_image("ghcr.io/creel-ai/executor-bad:latest")


class TestEnsureImageUncachedRemote:
    """Tests for _ensure_image_uncached with remote images."""

    @patch("subprocess.run")
    def test_remote_image_always_pulled(self, mock_run: MagicMock) -> None:
        """Should always pull remote images to pick up security patches."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = _ensure_image_uncached("ghcr.io/creel-ai/executor-weather:latest")
        assert result == "ghcr.io/creel-ai/executor-weather:latest"
        # docker pull should have been called
        assert mock_run.call_count == 1
        assert "pull" in mock_run.call_args[0][0]


class TestEnsureImageFromDockerfile:
    """Tests for _ensure_image_from_dockerfile."""

    @patch("subprocess.run")
    def test_image_already_exists(self, mock_run: MagicMock) -> None:
        """Should skip build if image already exists."""
        mock_run.return_value = MagicMock(returncode=0)
        with tempfile.NamedTemporaryFile(suffix="Dockerfile", delete=False) as f:
            f.write(b"FROM python:3.12-slim\n")
            f.flush()
            result = _ensure_image_from_dockerfile(f.name, "custom-tool:latest")
        assert result == "custom-tool:latest"

    def test_missing_dockerfile_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="Custom Dockerfile not found"):
            _ensure_image_from_dockerfile("/nonexistent/Dockerfile", "custom:latest")

    @patch("creel.containers._build_image")
    @patch("subprocess.run")
    def test_builds_from_custom_dockerfile(
        self, mock_run: MagicMock, mock_build: MagicMock
    ) -> None:
        """Should build using the custom Dockerfile when image doesn't exist."""
        mock_run.return_value = MagicMock(returncode=1)  # inspect fails
        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = Path(tmp) / "Dockerfile"
            dockerfile.write_text("FROM python:3.12-slim\n")
            result = _ensure_image_from_dockerfile(str(dockerfile), "custom-tool:latest")
        assert result == "custom-tool:latest"
        mock_build.assert_called_once()
        tags = mock_build.call_args[1].get("tags") or mock_build.call_args[0][0]
        assert "custom-tool:latest" in tags


class TestCollectRequiredImagesGHCR:
    """Tests for collect_required_images with GHCR and dockerfile support."""

    def test_remote_image_skips_base(self) -> None:
        """Remote images should not trigger inclusion of the local base image."""
        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "gmail": ToolConfig(
                    executor="gmail_readonly",
                    description="Gmail",
                    image="ghcr.io/creel-ai/executor-gmail-readonly:latest",
                ),
            },
        )
        images = collect_required_images(agent_def)
        assert "creel-executor-base:latest" not in images
        assert "ghcr.io/creel-ai/executor-gmail-readonly:latest" in images

    def test_dockerfile_generates_custom_tag(self) -> None:
        """Tools with dockerfile should use custom- prefix tag."""
        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "my_tool": ToolConfig(
                    executor="my_tool",
                    description="Custom",
                    dockerfile="./custom/Dockerfile",
                ),
            },
        )
        images = collect_required_images(agent_def)
        assert "custom-my-tool:latest" in images
        assert "creel-executor-base:latest" not in images

    def test_mixed_local_and_remote(self) -> None:
        """Mix of local and remote should include base for local only."""
        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "weather": ToolConfig(executor="weather", description="Weather"),
                "gmail": ToolConfig(
                    executor="gmail_readonly",
                    description="Gmail",
                    image="ghcr.io/creel-ai/executor-gmail-readonly:latest",
                ),
            },
        )
        images = collect_required_images(agent_def)
        assert "creel-executor-base:latest" in images
        assert "executor-weather:latest" in images
        assert "ghcr.io/creel-ai/executor-gmail-readonly:latest" in images


class TestPullRequiredImages:
    """Tests for pull_required_images."""

    @patch("creel.containers._pull_image")
    def test_pulls_only_remote_images(self, mock_pull: MagicMock) -> None:
        """Should only pull remote images, not local ones."""
        mock_pull.return_value = "ghcr.io/creel-ai/executor-weather:latest"
        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "weather": ToolConfig(
                    executor="weather",
                    description="Weather",
                    image="ghcr.io/creel-ai/executor-weather:latest",
                ),
                "local": ToolConfig(executor="local_tool", description="Local"),
            },
        )
        messages = pull_required_images(agent_def)
        assert any("pulled" in m for m in messages)
        # Should have pulled only the GHCR image
        mock_pull.assert_called_once_with("ghcr.io/creel-ai/executor-weather:latest")

    def test_no_remote_images_returns_message(self) -> None:
        """Should return informational message when no remote images exist."""
        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "weather": ToolConfig(executor="weather", description="Weather"),
            },
        )
        messages = pull_required_images(agent_def)
        assert any("No remote images" in m for m in messages)

    @patch("creel.containers._pull_image")
    def test_pull_failure_logs_warning(self, mock_pull: MagicMock) -> None:
        """Should log warning on pull failure, not raise."""
        mock_pull.side_effect = RuntimeError("network error")
        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "weather": ToolConfig(
                    executor="weather",
                    description="Weather",
                    image="ghcr.io/creel-ai/executor-weather:latest",
                ),
            },
        )
        messages = pull_required_images(agent_def)
        assert any("warning" in m for m in messages)


class TestToolConfigDockerfile:
    """Tests for ToolConfig.dockerfile field."""

    def test_dockerfile_field_default_none(self) -> None:
        tc = ToolConfig(executor="test", description="Test")
        assert tc.dockerfile is None

    def test_dockerfile_field_set(self) -> None:
        tc = ToolConfig(
            executor="test",
            description="Test",
            dockerfile="./my-executor/Dockerfile",
        )
        assert tc.dockerfile == "./my-executor/Dockerfile"

    def test_dockerfile_and_image_mutually_exclusive(self) -> None:
        """Setting both dockerfile and image should raise a validation error."""
        with pytest.raises(ValueError, match="mutually exclusive"):
            ToolConfig(
                executor="test",
                description="Test",
                image="some-image:latest",
                dockerfile="./Dockerfile",
            )
