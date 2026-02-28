"""Tests for content-hash Docker image tagging."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.orchestrator import _compute_executor_hash, _ensure_image, _image_cache


@pytest.fixture(autouse=True)
def _clear_image_cache():
    """Reset the module-level image build cache between tests."""
    _image_cache.clear()
    yield
    _image_cache.clear()


# ---------------------------------------------------------------------------
# _compute_executor_hash
# ---------------------------------------------------------------------------


class TestComputeExecutorHash:
    def test_deterministic(self, tmp_path: Path) -> None:
        """Same files should produce the same hash."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        (tmp_path / "executor.py").write_text("print('hello')")
        (tmp_path / "requirements.txt").write_text("requests==2.31")

        h1 = _compute_executor_hash(tmp_path)
        h2 = _compute_executor_hash(tmp_path)
        assert h1 == h2

    def test_length_is_12(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        assert len(_compute_executor_hash(tmp_path)) == 12

    def test_changes_when_file_content_changes(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        (tmp_path / "executor.py").write_text("v1")

        h1 = _compute_executor_hash(tmp_path)

        (tmp_path / "executor.py").write_text("v2")
        h2 = _compute_executor_hash(tmp_path)

        assert h1 != h2

    def test_changes_when_file_added(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        h1 = _compute_executor_hash(tmp_path)

        (tmp_path / "helper.py").write_text("pass")
        h2 = _compute_executor_hash(tmp_path)

        assert h1 != h2

    def test_ignores_non_matching_files(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        h1 = _compute_executor_hash(tmp_path)

        (tmp_path / "notes.md").write_text("ignored")
        h2 = _compute_executor_hash(tmp_path)

        assert h1 == h2

    def test_changes_when_shared_file_changes(self, tmp_path: Path) -> None:
        """Shared files in the parent (build context) affect the hash."""
        executor_dir = tmp_path / "weather"
        executor_dir.mkdir()
        (executor_dir / "Dockerfile").write_text("FROM python:3.12")
        (tmp_path / "google_creds.py").write_text("v1")

        h1 = _compute_executor_hash(executor_dir)

        (tmp_path / "google_creds.py").write_text("v2")
        h2 = _compute_executor_hash(executor_dir)

        assert h1 != h2

    def test_empty_dir(self, tmp_path: Path) -> None:
        """An empty directory still produces a valid 12-char hash."""
        h = _compute_executor_hash(tmp_path)
        assert len(h) == 12


# ---------------------------------------------------------------------------
# _ensure_image – executor images get content-hash tags
# ---------------------------------------------------------------------------


class TestEnsureImageContentHash:
    """Verify that executor images are tagged with a content hash."""

    def _setup_executor_dir(self, tmp_path: Path) -> Path:
        executor_dir = tmp_path / "src" / "executors" / "weather"
        executor_dir.mkdir(parents=True)
        (executor_dir / "Dockerfile").write_text("FROM python:3.12")
        (executor_dir / "executor.py").write_text("print('weather')")
        (executor_dir / "requirements.txt").write_text("requests")
        return executor_dir

    @patch("taskrunner.orchestrator.subprocess.run")
    def test_builds_with_hash_tag_when_missing(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """When no hashed image exists, build with hash + latest tags."""
        executor_dir = self._setup_executor_dir(tmp_path)
        content_hash = _compute_executor_hash(executor_dir)
        expected_image = f"executor-weather:{content_hash}"

        # inspect -> not found; build -> success
        mock_run.side_effect = [
            MagicMock(returncode=1),  # docker image inspect (not found)
            MagicMock(returncode=0, stderr="", stdout=""),  # docker build
        ]

        with patch("taskrunner.orchestrator.Path", side_effect=lambda x: tmp_path / x):
            result = _ensure_image("executor-weather:latest")

        assert result == expected_image

        # Verify build was called with both tags
        build_call = mock_run.call_args_list[1]
        build_args = build_call[0][0]
        assert "-t" in build_args
        tag_indices = [i for i, a in enumerate(build_args) if a == "-t"]
        tags = [build_args[i + 1] for i in tag_indices]
        assert expected_image in tags
        assert "executor-weather:latest" in tags

    @patch("taskrunner.orchestrator.subprocess.run")
    def test_skips_build_when_hash_exists(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """When the hashed image already exists, skip build."""
        self._setup_executor_dir(tmp_path)

        # inspect -> found
        mock_run.return_value = MagicMock(returncode=0)

        with patch("taskrunner.orchestrator.Path", side_effect=lambda x: tmp_path / x):
            result = _ensure_image("executor-weather:latest")

        assert result.startswith("executor-weather:")
        assert result != "executor-weather:latest"
        # Only one subprocess call (inspect), no build
        assert mock_run.call_count == 1

    @patch("taskrunner.orchestrator.subprocess.run")
    def test_non_executor_image_unchanged(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Non-executor images (e.g. llm-runner) pass through unchanged."""
        llm_dir = tmp_path / "src" / "llm"
        llm_dir.mkdir(parents=True)
        (llm_dir / "Dockerfile").write_text("FROM python:3.12")

        # inspect -> found
        mock_run.return_value = MagicMock(returncode=0)

        with patch("taskrunner.orchestrator.Path", side_effect=lambda x: tmp_path / x):
            result = _ensure_image("llm-runner:latest")

        assert result == "llm-runner:latest"

    @patch("taskrunner.orchestrator.subprocess.run")
    def test_missing_dockerfile_raises(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Missing Dockerfile should raise FileNotFoundError."""
        # Create dir but no Dockerfile
        (tmp_path / "src" / "executors" / "weather").mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=1)

        with patch("taskrunner.orchestrator.Path", side_effect=lambda x: tmp_path / x):
            with pytest.raises(FileNotFoundError):
                _ensure_image("executor-weather:latest")
