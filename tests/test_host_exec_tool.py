"""Tests for the host_exec executor and orchestrator integration."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from creel.models import ExecutorConfig


class TestHostExecExecutor:
    """Test the host_exec executor functions."""

    @patch("executors.host_exec.executor.requests")
    def test_host_exec_foreground(self, mock_requests):
        from executors.host_exec.executor import host_exec

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "session_id": "echo-1",
            "command": "echo hello",
            "status": "exited",
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
        }
        mock_response.raise_for_status = MagicMock()
        mock_requests.post.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_exec("echo hello", background=False, timeout=10)

        assert result["ok"] is True
        assert result["status"] == "exited"
        assert result["exit_code"] == 0

    @patch("executors.host_exec.executor.requests")
    def test_host_exec_background(self, mock_requests):
        from executors.host_exec.executor import host_exec

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "session_id": "sleep-1",
            "pid": 12345,
            "status": "running",
        }
        mock_response.raise_for_status = MagicMock()
        mock_requests.post.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_exec("sleep 30", background=True)

        assert result["ok"] is True
        assert result["status"] == "running"
        assert result["session_id"] == "sleep-1"

    @patch("executors.host_exec.executor.requests")
    def test_host_process_poll(self, mock_requests):
        from executors.host_exec.executor import host_process

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "session_id": "sleep-1",
            "status": "running",
            "pid": 12345,
        }
        mock_response.raise_for_status = MagicMock()
        mock_requests.post.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_process("sleep-1", "poll")

        assert result["ok"] is True
        assert result["status"] == "running"

    @patch("executors.host_exec.executor.requests")
    def test_host_process_log(self, mock_requests):
        from executors.host_exec.executor import host_process

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "session_id": "echo-1",
            "lines": ["[out] hello", "[out] world"],
        }
        mock_response.raise_for_status = MagicMock()
        mock_requests.post.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_process("echo-1", "log", limit=50)

        assert result["ok"] is True
        assert len(result["lines"]) == 2

    @patch("executors.host_exec.executor.requests")
    def test_host_sessions(self, mock_requests):
        from executors.host_exec.executor import host_sessions

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "sessions": [
                {"session_id": "sleep-1", "status": "running"},
                {"session_id": "echo-2", "status": "exited"},
            ],
        }
        mock_response.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_sessions()

        assert result["ok"] is True
        assert len(result["sessions"]) == 2

    def test_missing_bridge_url(self):
        from executors.host_exec.executor import host_exec

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_URL"):
                host_exec("echo hello")

    def test_missing_bridge_token(self):
        from executors.host_exec.executor import host_exec

        with patch.dict("os.environ", {"BRIDGE_URL": "http://localhost:8099"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_TOKEN"):
                host_exec("echo hello")


class TestOrchestratorIntegration:
    """Test that orchestrator dispatches host_exec tools correctly."""

    def test_host_exec_in_dispatch_table(self):
        from creel.orchestrator import _dispatch_executor

        # Verify the dispatch table has our new entries
        config = ExecutorConfig(name="host_exec", args={"command": "echo test"})
        with patch("creel.orchestrator._exec_host_exec_inline") as mock:
            mock.return_value = '{"ok": true}'
            _dispatch_executor("host_exec", config)
            mock.assert_called_once_with(config)

    def test_host_process_in_dispatch_table(self):
        from creel.orchestrator import _dispatch_executor

        config = ExecutorConfig(
            name="host_process", args={"session_id": "test-1", "action": "poll"}
        )
        with patch("creel.orchestrator._exec_host_process_inline") as mock:
            mock.return_value = '{"ok": true}'
            _dispatch_executor("host_process", config)
            mock.assert_called_once_with(config)

    def test_host_sessions_in_dispatch_table(self):
        from creel.orchestrator import _dispatch_executor

        config = ExecutorConfig(name="host_sessions", args={})
        with patch("creel.orchestrator._exec_host_sessions_inline") as mock:
            mock.return_value = '{"sessions": []}'
            _dispatch_executor("host_sessions", config)
            mock.assert_called_once_with(config)

    def test_bridge_scope_mapping(self):
        from creel.orchestrator import _EXECUTOR_TO_BRIDGE_SCOPE

        assert _EXECUTOR_TO_BRIDGE_SCOPE["host_exec"] == "EXEC"
        assert _EXECUTOR_TO_BRIDGE_SCOPE["host_process"] == "EXEC"
        assert _EXECUTOR_TO_BRIDGE_SCOPE["host_sessions"] == "EXEC"

    @patch("executors.host_exec.executor.host_exec")
    def test_exec_host_exec_inline(self, mock_host_exec):
        from creel.orchestrator import _exec_host_exec_inline

        mock_host_exec.return_value = {
            "ok": True,
            "session_id": "echo-1",
            "status": "exited",
            "exit_code": 0,
            "stdout": "hello\n",
        }

        config = ExecutorConfig(
            name="host_exec",
            args={
                "command": "echo hello",
                "background": "false",
                "workdir": "/tmp",
                "timeout": "30",
            },
        )

        result = _exec_host_exec_inline(config)
        parsed = json.loads(result)
        assert parsed["ok"] is True

        mock_host_exec.assert_called_once_with(
            "echo hello", background=False, workdir="/tmp", timeout=30, env=None
        )

    @patch("executors.host_exec.executor.host_process")
    def test_exec_host_process_inline(self, mock_host_process):
        from creel.orchestrator import _exec_host_process_inline

        mock_host_process.return_value = {
            "ok": True,
            "session_id": "test-1",
            "status": "running",
        }

        config = ExecutorConfig(
            name="host_process",
            args={
                "session_id": "test-1",
                "action": "poll",
            },
        )

        result = _exec_host_process_inline(config)
        parsed = json.loads(result)
        assert parsed["ok"] is True

        mock_host_process.assert_called_once_with("test-1", "poll", limit=100, offset=0, data=None)

    @patch("executors.host_exec.executor.host_sessions")
    def test_exec_host_sessions_inline(self, mock_host_sessions):
        from creel.orchestrator import _exec_host_sessions_inline

        mock_host_sessions.return_value = {"ok": True, "sessions": []}

        config = ExecutorConfig(name="host_sessions", args={})

        result = _exec_host_sessions_inline(config)
        parsed = json.loads(result)
        assert parsed["ok"] is True
        assert parsed["sessions"] == []

    def test_host_exec_missing_command(self):
        from creel.orchestrator import _exec_host_exec_inline

        config = ExecutorConfig(name="host_exec", args={})
        with pytest.raises(ValueError, match="'command' argument"):
            _exec_host_exec_inline(config)

    def test_host_process_missing_session_id(self):
        from creel.orchestrator import _exec_host_process_inline

        config = ExecutorConfig(name="host_process", args={"action": "poll"})
        with pytest.raises(ValueError, match="'session_id' argument"):
            _exec_host_process_inline(config)
