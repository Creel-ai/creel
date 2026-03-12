"""Tests for container infrastructure — base image, hash computation, and prebuilding."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from creel.containers import (
    _BASE_DOCKERFILE,
    _compute_base_image_hash,
    _compute_executor_hash,
    _ensure_base_image,
    collect_required_images,
    prebuild_images,
)


class TestBaseImageHash:
    """Tests for base image hash computation."""

    def test_hash_changes_when_base_dockerfile_changes(self) -> None:
        """Changing the base Dockerfile should produce a different hash."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = Path(tmp) / "Dockerfile"
            dockerfile.write_text("FROM python:3.12-slim\n")
            with patch("creel.containers._BASE_DOCKERFILE", dockerfile):
                hash1 = _compute_base_image_hash()

            dockerfile.write_text("FROM python:3.12-slim\nRUN echo changed\n")
            with patch("creel.containers._BASE_DOCKERFILE", dockerfile):
                hash2 = _compute_base_image_hash()

        assert hash1 != hash2

    def test_hash_is_12_chars(self) -> None:
        """Hash should be 12 hex characters."""
        h = _compute_base_image_hash()
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)


class TestEnsureBaseImage:
    """Tests for _ensure_base_image()."""

    @patch("subprocess.run")
    def test_skips_build_when_image_exists(self, mock_run: MagicMock) -> None:
        """Should skip building if the image already exists."""
        mock_run.return_value = MagicMock(returncode=0)  # docker inspect succeeds
        result = _ensure_base_image()
        assert result.startswith("creel-executor-base:")
        # Only docker inspect was called, not docker build
        assert mock_run.call_count == 1
        assert "inspect" in mock_run.call_args[0][0][2]

    @patch("creel.containers._build_image")
    @patch("subprocess.run")
    def test_builds_when_image_missing(self, mock_run: MagicMock, mock_build: MagicMock) -> None:
        """Should build the image when docker inspect fails."""
        mock_run.return_value = MagicMock(returncode=1)  # docker inspect fails
        result = _ensure_base_image()
        assert result.startswith("creel-executor-base:")
        mock_build.assert_called_once()
        tags = mock_build.call_args[1].get("tags") or mock_build.call_args[0][0]
        assert any("creel-executor-base:" in t for t in tags)


class TestExecutorHashIncludesBase:
    """Tests for _compute_executor_hash including base Dockerfile."""

    def test_hash_changes_when_base_dockerfile_changes(self) -> None:
        """Executor hash should change when the base Dockerfile changes."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Set up a minimal executor directory
            executor_dir = tmp_path / "executors" / "weather"
            executor_dir.mkdir(parents=True)
            (executor_dir / "Dockerfile").write_text("FROM base\n")
            (executor_dir / "executor.py").write_text("print('hello')\n")

            base_dockerfile = tmp_path / "base" / "Dockerfile"
            base_dockerfile.parent.mkdir(parents=True)
            base_dockerfile.write_text("FROM python:3.12-slim\n")

            with patch("creel.containers._BASE_DOCKERFILE", base_dockerfile):
                hash1 = _compute_executor_hash(executor_dir)

            base_dockerfile.write_text("FROM python:3.12-slim\nRUN echo changed\n")
            with patch("creel.containers._BASE_DOCKERFILE", base_dockerfile):
                hash2 = _compute_executor_hash(executor_dir)

        assert hash1 != hash2


class TestCollectRequiredImages:
    """Tests for collect_required_images including base image."""

    def test_includes_base_image_when_executors_present(self) -> None:
        """Should include the base image when executor images are needed."""
        from creel.models import AgentDefinition, ToolConfig

        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "weather": ToolConfig(executor="weather", description="Weather"),
            },
        )
        with patch("creel.containers._BASE_DOCKERFILE", _BASE_DOCKERFILE):
            images = collect_required_images(agent_def)
        assert "creel-executor-base:latest" in images

    def test_excludes_base_when_only_custom_images(self) -> None:
        """Should not include base image when all tools use custom images."""
        from creel.models import AgentDefinition, ToolConfig

        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "custom": ToolConfig(
                    executor="custom", description="Custom", image="my-image:latest"
                ),
            },
        )
        images = collect_required_images(agent_def)
        assert "creel-executor-base:latest" not in images


class TestPrebuildOrder:
    """Tests for prebuild_images building base first."""

    @patch("creel.containers._image_cache")
    @patch("creel.containers._ensure_base_image")
    def test_base_built_synchronously_first(
        self, mock_ensure_base: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Base image should be built synchronously before parallel executor builds."""
        from creel.models import AgentDefinition, ToolConfig

        agent_def = AgentDefinition(
            system_prompt="test",
            tools={
                "weather": ToolConfig(executor="weather", description="Weather"),
            },
        )

        mock_cache.start_prebuild.return_value = []

        prebuild_images(agent_def)

        # _ensure_base_image should have been called
        mock_ensure_base.assert_called_once()

        # start_prebuild should not include the base image
        prebuild_call = mock_cache.start_prebuild.call_args[0][0]
        assert "creel-executor-base:latest" not in prebuild_call
