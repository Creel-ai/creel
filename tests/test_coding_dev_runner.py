"""Tests for the coding executor dev_runner keepalive protocol and auto-setup."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from executors.coding.executor import _SETUP_DONE_MARKER, _setup_cache, detect_and_setup


class TestDetectAndSetup:
    """Tests for project type detection and auto-install."""

    def setup_method(self) -> None:
        _setup_cache.clear()

    def test_detects_package_json(self, tmp_path: Path) -> None:
        """Should detect a Node.js project and attempt npm ci."""
        (tmp_path / "package.json").write_text('{"name": "test"}')
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = detect_and_setup(str(tmp_path))
        assert result["detected"] == "package.json"
        assert result["installed"] is True
        assert result["error"] is None

    def test_detects_requirements_txt(self, tmp_path: Path) -> None:
        """Should detect a Python project with requirements.txt."""
        (tmp_path / "requirements.txt").write_text("httpx\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = detect_and_setup(str(tmp_path))
        assert result["detected"] == "requirements.txt"
        assert result["installed"] is True

    def test_detects_pyproject_toml(self, tmp_path: Path) -> None:
        """Should detect a Python project with pyproject.toml."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = detect_and_setup(str(tmp_path))
        assert result["detected"] == "pyproject.toml"

    def test_detects_cargo_toml(self, tmp_path: Path) -> None:
        """Should detect a Rust project."""
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"\n')
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = detect_and_setup(str(tmp_path))
        assert result["detected"] == "Cargo.toml"

    def test_detects_go_mod(self, tmp_path: Path) -> None:
        """Should detect a Go project."""
        (tmp_path / "go.mod").write_text("module example.com/test\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = detect_and_setup(str(tmp_path))
        assert result["detected"] == "go.mod"

    def test_no_project_detected(self, tmp_path: Path) -> None:
        """Should return gracefully when no project manifest found."""
        result = detect_and_setup(str(tmp_path))
        assert result["detected"] is None
        assert result["installed"] is False
        assert result["error"] is None

    def test_idempotent_via_marker_file(self, tmp_path: Path) -> None:
        """Should not re-run setup if marker file exists."""
        (tmp_path / "package.json").write_text('{"name": "test"}')
        (tmp_path / _SETUP_DONE_MARKER).write_text("done\n")
        result = detect_and_setup(str(tmp_path))
        assert result["detected"] == "already-setup"
        assert result["installed"] is True

    def test_idempotent_via_cache(self, tmp_path: Path) -> None:
        """Should not re-run setup if workspace already cached in-memory."""
        (tmp_path / "package.json").write_text('{"name": "test"}')
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            detect_and_setup(str(tmp_path))
            detect_and_setup(str(tmp_path))
        # Only called once
        mock_run.assert_called_once()

    def test_graceful_failure(self, tmp_path: Path) -> None:
        """Should report error but not crash on failed install."""
        (tmp_path / "package.json").write_text('{"name": "bad"}')
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="npm ERR! something")
            result = detect_and_setup(str(tmp_path))
        assert result["detected"] == "package.json"
        assert result["installed"] is False
        assert "npm ERR!" in result["error"]

    def test_priority_package_json_over_pyproject(self, tmp_path: Path) -> None:
        """package.json should take priority over pyproject.toml."""
        (tmp_path / "package.json").write_text('{"name": "test"}')
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = detect_and_setup(str(tmp_path))
        assert result["detected"] == "package.json"

    def test_writes_marker_file_on_success(self, tmp_path: Path) -> None:
        """Should write marker file after successful setup."""
        (tmp_path / "requirements.txt").write_text("httpx\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            detect_and_setup(str(tmp_path))
        assert (tmp_path / _SETUP_DONE_MARKER).exists()


class TestDevRunnerProtocol:
    """Tests for the dev_runner.py JSON-over-stdio protocol.

    Tests the protocol handler by simulating stdin/stdout.
    """

    def _run_dev_runner(self, messages: list[dict]) -> list[dict]:
        """Helper: feed messages to dev_runner.main() and return output lines."""
        import io

        # Build stdin with all messages followed by shutdown
        lines = [json.dumps(m) for m in messages]
        lines.append(json.dumps({"type": "shutdown"}))
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()

        # Import after patching to handle the 'from executor import ...' at module level
        import executors.coding.dev_runner as dr

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            dr.main()

        stdout.seek(0)
        return [json.loads(line) for line in stdout.readlines() if line.strip()]

    def test_ping_pong(self) -> None:
        """Ping should return pong."""
        results = self._run_dev_runner([{"type": "ping"}])
        assert results[0]["type"] == "pong"

    def test_reset_sends_ready(self) -> None:
        """Reset should return ready."""
        results = self._run_dev_runner([{"type": "reset"}])
        assert results[0]["type"] == "ready"

    def test_execute_returns_result(self) -> None:
        """Execute should return a result dict."""
        with (
            patch("executors.coding.dev_runner.run_command") as mock_run,
            patch("executors.coding.dev_runner.detect_and_setup") as mock_setup,
        ):
            mock_setup.return_value = {"detected": None, "installed": False, "error": None}
            mock_run.return_value = {
                "command": "echo hello",
                "exit_code": 0,
                "stdout": "hello\n",
                "stderr": "",
                "success": True,
            }
            results = self._run_dev_runner(
                [{"type": "execute", "command": "echo hello", "workdir": "/tmp"}]
            )
        assert results[0]["type"] == "result"
        assert results[0]["success"] is True

    def test_shutdown_exits(self) -> None:
        """Shutdown should cause the loop to exit cleanly."""
        results = self._run_dev_runner([])  # Just shutdown
        assert results == []

    def test_unknown_type_returns_error(self) -> None:
        """Unknown message type should return an error."""
        results = self._run_dev_runner([{"type": "bogus"}])
        assert results[0]["type"] == "error"
        assert "Unknown" in results[0]["message"]


class TestCodingPoolRouting:
    """Tests for routing coding executor through the warm container pool."""

    def test_pool_acquire_execute_release(self) -> None:
        """Should acquire a container, send execute, and release."""
        from creel.tools import _run_coding_via_pool

        mock_pool = MagicMock()
        mock_container = MagicMock()
        mock_pool.acquire.return_value = mock_container
        mock_pool.enabled = True
        mock_container.recv.return_value = {
            "type": "result",
            "command": "echo hi",
            "exit_code": 0,
            "stdout": "hi\n",
            "stderr": "",
            "success": True,
        }

        from creel.models import ExecutorConfig, ToolConfig

        executor_config = ExecutorConfig(name="coding", args={"COMMAND": "echo hi"}, timeout=300)
        tool_config = ToolConfig(
            executor="coding",
            description="Dev",
            writable=True,
            memory="512m",
            cpus="1.0",
            tmpfs_size="256M",
            network=True,
        )

        with patch("creel.containers._ensure_image", return_value="executor-coding:abc123"):
            result = _run_coding_via_pool(mock_pool, executor_config, tool_config)

        mock_pool.acquire.assert_called_once()
        mock_container.send.assert_called_once()
        mock_pool.release.assert_called_once_with(mock_container)
        parsed = json.loads(result)
        assert parsed["success"] is True

    def test_pool_fallback_on_error(self) -> None:
        """Should force-kill container on error and not release to pool."""
        from creel.tools import _run_coding_via_pool

        mock_pool = MagicMock()
        mock_container = MagicMock()
        mock_pool.acquire.return_value = mock_container
        mock_container.recv.side_effect = TimeoutError("timed out")

        from creel.models import ExecutorConfig, ToolConfig

        executor_config = ExecutorConfig(name="coding", args={"COMMAND": "hang"}, timeout=5)
        tool_config = ToolConfig(
            executor="coding",
            description="Dev",
            writable=True,
            memory="512m",
            cpus="1.0",
            network=True,
        )

        with (
            patch("creel.containers._ensure_image", return_value="executor-coding:abc123"),
            pytest.raises(TimeoutError),
        ):
            _run_coding_via_pool(mock_pool, executor_config, tool_config)

        mock_container.force_kill.assert_called_once()
        mock_pool.release.assert_not_called()
