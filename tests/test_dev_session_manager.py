"""Tests for DevSessionManager host-side container management."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from creel.models import ExecutorConfig, ToolConfig


def _make_tool_config(**kwargs) -> ToolConfig:
    """Create a ToolConfig with sensible defaults."""
    defaults = {
        "executor": "dev_session",
        "description": "Dev session container",
        "image": "executor-dev-session:latest",
        "writable": True,
        "memory": "512m",
        "cpus": "1.0",
        "tmpfs_size": "256M",
        "network": True,
    }
    defaults.update(kwargs)
    return ToolConfig(**defaults)


def _make_container_mock(alive: bool = True) -> MagicMock:
    """Create a ManagedContainer mock."""
    container = MagicMock()
    container.alive = alive
    container.ping.return_value = True
    container.container_name = "creel-devsession-abc123"
    container.env_file_path = "/tmp/creel-devsession-test.env"
    return container


class TestLifecycle:
    """Test container lifecycle management."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_lazy_container_start(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "sessions_result",
            "sessions": [],
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "sessions"},
            )
            mgr.execute(config, _make_tool_config())

            # Container should have been started
            mock_ensure.assert_called_once()
            mock_subprocess.Popen.assert_called_once()
            container.ping.assert_called_once()
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_container_reused(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {"type": "sessions_result", "sessions": []}

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
            tc = _make_tool_config()

            mgr.execute(config, tc)
            mgr.execute(config, tc)

            # Should only have started the container once
            assert mock_subprocess.Popen.call_count == 1
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_container_restart_after_death(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container1 = _make_container_mock()
        container2 = _make_container_mock()
        mock_mc_cls.side_effect = [container1, container2]

        container1.recv.return_value = {"type": "sessions_result", "sessions": []}
        container2.recv.return_value = {"type": "sessions_result", "sessions": []}

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
            tc = _make_tool_config()

            # First call starts the container
            mgr.execute(config, tc)

            # Simulate container death
            container1.alive = False

            # Second call should restart
            mgr.execute(config, tc)

            assert mock_subprocess.Popen.call_count == 2
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_health_check_failure(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        container.ping.return_value = False
        mock_mc_cls.return_value = container

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
            with pytest.raises(RuntimeError, match="health check"):
                mgr.execute(config, _make_tool_config())

            container.force_kill.assert_called_once()
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    def test_shutdown_cleans_up(self, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        mgr = DevSessionManager()
        container = _make_container_mock()
        mgr._container = container
        mgr._container_name = "creel-devsession-test"

        mgr.shutdown()

        container.shutdown.assert_called_once()
        assert mgr._container is None
        assert mgr._closed is True


class TestExec:
    """Test dev_exec dispatch."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_exec_foreground(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "exec_result",
            "session_id": "echo-1",
            "status": "exited",
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "exec", "command": "echo hello", "timeout": "30"},
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)

            assert parsed["type"] == "exec_result"
            assert parsed["exit_code"] == 0

            # Verify the sent message
            sent_msg = container.send.call_args[0][0]
            assert sent_msg["type"] == "exec"
            assert sent_msg["command"] == "echo hello"
            assert sent_msg["background"] is False
            assert sent_msg["timeout"] == 30
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_exec_background(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "exec_result",
            "session_id": "npm-1",
            "status": "running",
            "pid": 12345,
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={
                    "_action": "exec",
                    "command": "npm run dev",
                    "background": "true",
                    "workdir": "/workspace",
                },
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)

            assert parsed["status"] == "running"

            sent_msg = container.send.call_args[0][0]
            assert sent_msg["background"] is True
            assert sent_msg["workdir"] == "/workspace"
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_exec_missing_command(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "exec"},
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_exec_recv_failure(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.side_effect = TimeoutError("timed out")

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "exec", "command": "sleep 9999"},
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            mgr.shutdown()


class TestProcess:
    """Test dev_process dispatch."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_process_poll(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "process_result",
            "session_id": "uvicorn-1",
            "status": "running",
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "process", "session_id": "uvicorn-1", "action": "poll"},
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)

            assert parsed["type"] == "process_result"

            sent_msg = container.send.call_args[0][0]
            assert sent_msg["type"] == "process"
            assert sent_msg["session_id"] == "uvicorn-1"
            assert sent_msg["action"] == "poll"
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_process_log_with_params(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "process_result",
            "session_id": "uvicorn-1",
            "lines": ["line1", "line2"],
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={
                    "_action": "process",
                    "session_id": "uvicorn-1",
                    "action": "log",
                    "limit": "50",
                    "offset": "10",
                },
            )
            mgr.execute(config, _make_tool_config())

            sent_msg = container.send.call_args[0][0]
            assert sent_msg["limit"] == 50
            assert sent_msg["offset"] == 10
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_process_write(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "process_result",
            "session_id": "node-1",
            "written": 6,
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={
                    "_action": "process",
                    "session_id": "node-1",
                    "action": "write",
                    "data": "hello\n",
                },
            )
            mgr.execute(config, _make_tool_config())

            sent_msg = container.send.call_args[0][0]
            assert sent_msg["data"] == "hello\n"
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_process_missing_session_id(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "process", "action": "poll"},
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            mgr.shutdown()


class TestSessions:
    """Test dev_sessions dispatch."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_list_sessions(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "sessions_result",
            "sessions": [
                {"session_id": "uvicorn-1", "status": "running"},
                {"session_id": "npm-1", "status": "exited"},
            ],
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)

            assert parsed["type"] == "sessions_result"
            assert len(parsed["sessions"]) == 2
        finally:
            mgr.shutdown()


class TestSingleton:
    """Test the module-level singleton pattern."""

    def test_get_returns_same_instance(self):
        import creel.dev_session_manager as mod

        # Reset singleton
        mod._manager = None

        try:
            mgr1 = mod.get_dev_session_manager()
            mgr2 = mod.get_dev_session_manager()
            assert mgr1 is mgr2
        finally:
            mod.shutdown_dev_session_manager()

    def test_shutdown_clears_singleton(self):
        import creel.dev_session_manager as mod

        mod._manager = None
        mod.get_dev_session_manager()
        mod.shutdown_dev_session_manager()
        assert mod._manager is None


class TestUnknownAction:
    """Test unknown action handling."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_unknown_action(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {"type": "sessions_result", "sessions": []}

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "bogus"},
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)
            assert "error" in parsed
            assert "Unknown" in parsed["error"]
        finally:
            mgr.shutdown()


class TestDockerFlags:
    """Test Docker flags passed to Popen."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_network_none_flag(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {"type": "sessions_result", "sessions": []}

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
            tc = _make_tool_config(network=False)
            mgr.execute(config, tc)

            popen_args = mock_subprocess.Popen.call_args[0][0]
            assert "--network=none" in popen_args
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_pids_limit_flag(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {"type": "sessions_result", "sessions": []}

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
            mgr.execute(config, _make_tool_config())

            popen_args = mock_subprocess.Popen.call_args[0][0]
            assert "--pids-limit=256" in popen_args
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_metadata_blocking(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {"type": "sessions_result", "sessions": []}

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
            mgr.execute(config, _make_tool_config())

            popen_args = mock_subprocess.Popen.call_args[0][0]
            assert "--add-host=169.254.169.254:127.0.0.1" in popen_args
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_volume_mounts(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager
        from creel.models import MountConfig

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {"type": "sessions_result", "sessions": []}

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
            tc = _make_tool_config(mounts=[MountConfig(path="/tmp/testproj", mode="rw")])
            mgr.execute(config, tc)

            popen_args = mock_subprocess.Popen.call_args[0][0]
            assert "-v" in popen_args
            # Find the volume arg following -v
            v_idx = popen_args.index("-v")
            vol_arg = popen_args[v_idx + 1]
            assert "/tmp/testproj" in vol_arg
            assert vol_arg.endswith(":rw")
        finally:
            mgr.shutdown()


class TestRecvFailures:
    """Test recv failure handling across dispatch methods."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_process_recv_failure(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        # First recv is for ping (handled by mock), second is for the actual call
        container.recv.side_effect = [TimeoutError("timed out")]

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "process", "session_id": "test-1", "action": "poll"},
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_sessions_recv_failure(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.side_effect = [TimeoutError("timed out")]

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "sessions"},
            )
            result = mgr.execute(config, _make_tool_config())
            parsed = json.loads(result)
            assert "error" in parsed
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_recv_failure_marks_unhealthy(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container1 = _make_container_mock()
        container2 = _make_container_mock()
        mock_mc_cls.side_effect = [container1, container2]

        # First call: recv fails (marks unhealthy)
        container1.recv.side_effect = TimeoutError("timed out")
        # Second call: succeeds (after restart)
        container2.recv.return_value = {
            "type": "exec_result",
            "session_id": "test-1",
            "status": "exited",
            "exit_code": 0,
            "stdout": "ok\n",
            "stderr": "",
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "exec", "command": "echo ok"},
            )
            tc = _make_tool_config()

            # First call fails — marks container unhealthy
            result1 = mgr.execute(config, tc)
            parsed1 = json.loads(result1)
            assert "error" in parsed1

            # Second call should restart (Popen called twice)
            result2 = mgr.execute(config, tc)
            parsed2 = json.loads(result2)
            assert parsed2["exit_code"] == 0

            assert mock_subprocess.Popen.call_count == 2
        finally:
            mgr.shutdown()


class TestSafeInt:
    """Test _safe_int fallback for invalid numeric arguments."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_exec_invalid_timeout_uses_default(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "exec_result",
            "session_id": "test-1",
            "status": "exited",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={"_action": "exec", "command": "echo hi", "timeout": "abc"},
            )
            mgr.execute(config, _make_tool_config())

            sent_msg = container.send.call_args[0][0]
            assert sent_msg["timeout"] == 300
        finally:
            mgr.shutdown()

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_process_invalid_limit_uses_default(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        container = _make_container_mock()
        mock_mc_cls.return_value = container
        container.recv.return_value = {
            "type": "process_result",
            "session_id": "test-1",
            "status": "running",
        }

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(
                name="dev_session",
                args={
                    "_action": "process",
                    "session_id": "test-1",
                    "action": "poll",
                    "limit": "abc",
                },
            )
            mgr.execute(config, _make_tool_config())

            sent_msg = container.send.call_args[0][0]
            assert sent_msg["limit"] == 100
        finally:
            mgr.shutdown()


class TestClosedState:
    """Test behavior after shutdown."""

    def test_start_after_shutdown_raises(self):
        from creel.dev_session_manager import DevSessionManager

        mgr = DevSessionManager()
        mgr.shutdown()

        config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})
        with pytest.raises(RuntimeError, match="shut down"):
            mgr.execute(config, _make_tool_config())

    def test_double_shutdown_is_safe(self):
        from creel.dev_session_manager import DevSessionManager

        mgr = DevSessionManager()
        mgr.shutdown()
        mgr.shutdown()  # Should not raise


class TestReaper:
    """Test idle reaper cleanup logic."""

    @patch("creel.dev_session_manager.subprocess")
    def test_reaper_cleans_dead_container(self, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        mgr = DevSessionManager()
        try:
            container = _make_container_mock(alive=False)
            mgr._container = container
            mgr._container_name = "creel-devsession-dead"
            mgr._env_file_path = "/tmp/creel-devsession-dead.env"
            mgr._started_at = 0.0

            # Directly invoke _cleanup_container (the path the reaper takes)
            mgr._cleanup_container()

            container.shutdown.assert_called_once()
            assert mgr._container is None
        finally:
            mgr.shutdown()


class TestPopenFailure:
    """Test Popen failure cleanup."""

    @patch("creel.dev_session_manager.subprocess")
    @patch("creel.dev_session_manager._ensure_image", return_value="executor-dev-session:abc123")
    @patch("creel.dev_session_manager.ManagedContainer")
    def test_popen_failure_cleans_env_file(self, mock_mc_cls, mock_ensure, mock_subprocess):
        from creel.dev_session_manager import DevSessionManager

        mock_subprocess.Popen.side_effect = OSError("docker not found")
        mock_subprocess.PIPE = subprocess.PIPE

        mgr = DevSessionManager()
        try:
            config = ExecutorConfig(name="dev_session", args={"_action": "sessions"})

            with patch.object(DevSessionManager, "_unlink") as mock_unlink:
                with pytest.raises(OSError, match="docker not found"):
                    mgr.execute(config, _make_tool_config())

                mock_unlink.assert_called_once()
                # The argument should be a path string (the env file)
                assert mock_unlink.call_args[0][0].endswith(".env")
        finally:
            mgr.shutdown()
