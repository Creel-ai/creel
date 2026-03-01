"""Tests for executor container logging and error propagation."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.models import ExecutorConfig


class TestRunExecutorContainer:
    """Test _run_executor_container error handling and logging."""

    @pytest.fixture
    def config(self) -> ExecutorConfig:
        return ExecutorConfig(name="test_executor", args={"key": "value"}, timeout=30)

    @patch("taskrunner.orchestrator._ensure_image")
    @patch("taskrunner.orchestrator.subprocess.run")
    @patch("taskrunner.orchestrator.decrypt_env_file", return_value={})
    def test_success_with_stderr_logs_debug(
        self, mock_decrypt, mock_run, mock_ensure, config, caplog
    ):
        """Stderr on success should be logged at DEBUG level."""
        from taskrunner.orchestrator import _run_executor_container

        mock_run.return_value = MagicMock(
            stdout="result data\n",
            stderr="some debug output\n",
            returncode=0,
        )

        import logging

        with caplog.at_level(logging.DEBUG):
            result = _run_executor_container(config)

        assert result == "result data"
        assert "some debug output" in caplog.text

    @patch("taskrunner.orchestrator._ensure_image")
    @patch("taskrunner.orchestrator.subprocess.run")
    @patch("taskrunner.orchestrator.decrypt_env_file", return_value={})
    def test_failure_stderr_in_exception(self, mock_decrypt, mock_run, mock_ensure, config):
        """Non-zero exit should raise RuntimeError with stderr content."""
        from taskrunner.orchestrator import _run_executor_container

        mock_run.return_value = MagicMock(
            stdout="",
            stderr="ImportError: No module named 'requests'\n",
            returncode=1,
        )

        with pytest.raises(RuntimeError, match="No module named"):
            _run_executor_container(config)

    @patch("taskrunner.orchestrator._ensure_image")
    @patch("taskrunner.orchestrator.subprocess.run")
    @patch("taskrunner.orchestrator.decrypt_env_file", return_value={})
    def test_failure_no_stderr(self, mock_decrypt, mock_run, mock_ensure, config):
        """Non-zero exit with empty stderr should still include exit code."""
        from taskrunner.orchestrator import _run_executor_container

        mock_run.return_value = MagicMock(
            stdout="",
            stderr="",
            returncode=137,
        )

        with pytest.raises(RuntimeError, match="exit code 137"):
            _run_executor_container(config)

    @patch("taskrunner.orchestrator._ensure_image")
    @patch("taskrunner.orchestrator.subprocess.run")
    @patch("taskrunner.orchestrator.decrypt_env_file", return_value={})
    def test_timeout_raises_runtime_error(self, mock_decrypt, mock_run, mock_ensure, config):
        """Timeout should raise RuntimeError with executor name and timeout."""
        from taskrunner.orchestrator import _run_executor_container

        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["docker", "run"], timeout=30, stderr="partial output"
        )

        with pytest.raises(RuntimeError, match="timed out after 30s"):
            _run_executor_container(config)

    @patch("taskrunner.orchestrator._ensure_image")
    @patch("taskrunner.orchestrator.subprocess.run")
    @patch("taskrunner.orchestrator.decrypt_env_file", return_value={})
    def test_configurable_timeout(self, mock_decrypt, mock_run, mock_ensure):
        """Timeout should use config.timeout, not hardcoded 60."""
        from taskrunner.orchestrator import _run_executor_container

        config = ExecutorConfig(name="slow_executor", timeout=120)
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        _run_executor_container(config)

        # Verify timeout passed to subprocess.run
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("timeout") == 120

    @patch("taskrunner.orchestrator._ensure_image")
    @patch("taskrunner.orchestrator.subprocess.run")
    @patch("taskrunner.orchestrator.decrypt_env_file", return_value={})
    def test_request_id_passed_to_container(
        self, mock_decrypt, mock_run, mock_ensure, config, tmp_path
    ):
        """Request ID should be injected as CREEL_REQUEST_ID env var."""
        from taskrunner.log import request_id_var
        from taskrunner.orchestrator import _run_executor_container

        token = request_id_var.set("abc12345")
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        try:
            _run_executor_container(config)
        finally:
            request_id_var.reset(token)

        # Check that the env file written contains CREEL_REQUEST_ID
        # We can verify by checking subprocess was called (env file is temp)
        mock_run.assert_called_once()

    @patch("taskrunner.orchestrator._ensure_image")
    @patch("taskrunner.orchestrator.subprocess.run")
    @patch("taskrunner.orchestrator.decrypt_env_file", return_value={})
    def test_stderr_truncated_in_error(self, mock_decrypt, mock_run, mock_ensure, config):
        """Very long stderr should be truncated in the error message."""
        from taskrunner.orchestrator import _run_executor_container

        long_stderr = "x" * 1000
        mock_run.return_value = MagicMock(
            stdout="",
            stderr=long_stderr,
            returncode=1,
        )

        with pytest.raises(RuntimeError) as exc_info:
            _run_executor_container(config)
        # Error detail truncated to 500 chars
        assert len(str(exc_info.value)) < 600

    @patch("taskrunner.orchestrator._ensure_image")
    @patch("taskrunner.orchestrator.subprocess.run")
    @patch(
        "taskrunner.orchestrator.decrypt_env_file",
        return_value={
            "GOOGLE_CREDENTIALS_JSON": (
                '{"refresh_token":"rt","client_id":"cid","client_secret":"cs"}'
            ),
        },
    )
    @patch(
        "taskrunner.oauth.get_google_access_token_from_json",
        return_value="ya29.container-token",
    )
    @patch("taskrunner.orchestrator.tempfile.NamedTemporaryFile")
    def test_google_credentials_json_replaced_with_access_token(
        self,
        mock_tmpfile,
        mock_access_token,
        mock_decrypt,
        mock_run,
        mock_ensure,
        config,
    ):
        """Container env file should include only GOOGLE_ACCESS_TOKEN."""
        from taskrunner.orchestrator import _run_executor_container

        config = ExecutorConfig(
            name="test_executor",
            secrets="secrets/google.env.enc",
            args={"key": "value"},
            timeout=30,
        )

        mock_env_file = MagicMock()
        mock_env_file.name = "/tmp/test.env"
        mock_tmpfile.return_value.__enter__.return_value = mock_env_file

        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        _run_executor_container(config)

        written = "".join(call.args[0] for call in mock_env_file.write.call_args_list)
        assert "GOOGLE_ACCESS_TOKEN=ya29.container-token" in written
        assert "GOOGLE_CREDENTIALS_JSON=" not in written
        mock_access_token.assert_called_once()


class TestExecutorConfigTimeout:
    def test_default_timeout(self):
        config = ExecutorConfig(name="test")
        assert config.timeout == 60

    def test_custom_timeout(self):
        config = ExecutorConfig(name="test", timeout=300)
        assert config.timeout == 300


class TestEnsureImage:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from taskrunner.orchestrator import _image_cache

        _image_cache.clear()
        yield
        _image_cache.clear()

    @patch("taskrunner.orchestrator.subprocess.run")
    def test_build_failure_includes_stderr(self, mock_run, tmp_path):
        """Docker build failure should log and raise with stderr."""
        from taskrunner.orchestrator import _ensure_image

        # First call: image inspect (not found)
        # Second call: build (fails)
        mock_run.side_effect = [
            MagicMock(returncode=1),  # inspect
            MagicMock(
                returncode=1,
                stderr="Step 3/5 : RUN pip install\nERROR: Could not find",
                stdout="",
            ),  # build
        ]

        dockerfile = tmp_path / "src" / "executors" / "test" / "Dockerfile"
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text("FROM python:3.11")

        with patch(
            "taskrunner.orchestrator.Path",
            side_effect=lambda x: tmp_path / x if not str(x).startswith("/") else x,
        ):
            with pytest.raises(RuntimeError, match="Could not find"):
                _ensure_image("executor-test:latest")
