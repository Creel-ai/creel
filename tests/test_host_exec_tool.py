"""Tests for the host_exec executor and orchestrator integration."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from creel.models import ExecutorConfig


class TestHostExecExecutor:
    """Test the host_exec executor functions."""

    @patch("executors.host_exec.executor.httpx")
    def test_host_exec_foreground(self, mock_httpx):
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
        mock_httpx.post.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_exec("echo hello", background=False, timeout=10)

        assert result["ok"] is True
        assert result["status"] == "exited"
        assert result["exit_code"] == 0

    @patch("executors.host_exec.executor.httpx")
    def test_host_exec_background(self, mock_httpx):
        from executors.host_exec.executor import host_exec

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "session_id": "sleep-1",
            "pid": 12345,
            "status": "running",
        }
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_exec("sleep 30", background=True)

        assert result["ok"] is True
        assert result["status"] == "running"
        assert result["session_id"] == "sleep-1"

    @patch("executors.host_exec.executor.httpx")
    def test_host_process_poll(self, mock_httpx):
        from executors.host_exec.executor import host_process

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "session_id": "sleep-1",
            "status": "running",
            "pid": 12345,
        }
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_process("sleep-1", "poll")

        assert result["ok"] is True
        assert result["status"] == "running"

    @patch("executors.host_exec.executor.httpx")
    def test_host_process_log(self, mock_httpx):
        from executors.host_exec.executor import host_process

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "session_id": "echo-1",
            "lines": ["[out] hello", "[out] world"],
        }
        mock_response.raise_for_status = MagicMock()
        mock_httpx.post.return_value = mock_response

        with (
            patch.dict(
                "os.environ", {"BRIDGE_URL": "http://localhost:8099", "BRIDGE_TOKEN": "tok"}
            ),
        ):
            result = host_process("echo-1", "log", limit=50)

        assert result["ok"] is True
        assert len(result["lines"]) == 2

    @patch("executors.host_exec.executor.httpx")
    def test_host_sessions(self, mock_httpx):
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
        mock_httpx.get.return_value = mock_response

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


class TestSkillRegistration:
    """Test that host_exec skill is registered correctly."""

    def test_register_skill_returns_valid_meta(self):
        from executors.host_exec.executor import register_skill

        meta, execute = register_skill()
        assert meta.id == "host_exec"
        assert meta.bridge_scope == "EXEC"
        assert callable(execute)
        tool_names = [t.name for t in meta.tools]
        assert "host_exec" in tool_names
        assert "host_process" in tool_names
        assert "host_sessions" in tool_names

    @patch("executors.host_exec.executor.host_exec")
    def test_host_exec_execute(self, mock_host_exec):
        from executors.host_exec.executor import register_skill

        mock_host_exec.return_value = {"ok": True, "stdout": "hello\n"}
        _, execute = register_skill()
        config = ExecutorConfig(
            name="host_exec",
            args={"_action": "exec", "command": "echo hello", "workdir": "/tmp", "timeout": "30"},
        )
        result = execute(config)
        parsed = json.loads(result)
        assert parsed["ok"] is True

    @patch("executors.host_exec.executor.host_process")
    def test_host_process_execute(self, mock_host_process):
        from executors.host_exec.executor import register_skill

        mock_host_process.return_value = {"ok": True, "status": "running"}
        _, execute = register_skill()
        config = ExecutorConfig(
            name="host_exec",
            args={"_action": "process", "session_id": "test-1", "action": "poll"},
        )
        result = execute(config)
        parsed = json.loads(result)
        assert parsed["ok"] is True

    @patch("executors.host_exec.executor.host_sessions")
    def test_host_sessions_execute(self, mock_host_sessions):
        from executors.host_exec.executor import register_skill

        mock_host_sessions.return_value = {"ok": True, "sessions": []}
        _, execute = register_skill()
        config = ExecutorConfig(name="host_exec", args={"_action": "sessions"})
        result = execute(config)
        parsed = json.loads(result)
        assert parsed["sessions"] == []

    def test_host_exec_missing_command(self):
        from executors.host_exec.executor import register_skill

        _, execute = register_skill()
        config = ExecutorConfig(name="host_exec", args={"_action": "exec"})
        with pytest.raises(ValueError, match="'command' argument"):
            execute(config)

    def test_host_process_missing_session_id(self):
        from executors.host_exec.executor import register_skill

        _, execute = register_skill()
        config = ExecutorConfig(name="host_exec", args={"_action": "process"})
        with pytest.raises(ValueError, match="'session_id' argument"):
            execute(config)
