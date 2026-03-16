"""Tests for the host-side interactive session manager.

Tests InteractiveSessionManager by mocking ManagedContainer and subprocess
to verify container lifecycle, security flags, and session routing.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from creel.interactive_sessions import InteractiveSessionManager
from creel.models import ExecutorConfig, ToolConfig


@pytest.fixture()
def manager():
    """Create a fresh session manager for each test, with reaper disabled."""
    mgr = InteractiveSessionManager()
    # Cancel the idle reaper to avoid interference in tests
    if mgr._reaper:
        mgr._reaper.cancel()
    yield mgr
    mgr.shutdown()


def _tool_config(**overrides) -> ToolConfig:
    """Build a minimal ToolConfig for exec_interactive."""
    defaults = {
        "executor": "exec_interactive",
        "description": "test",
        "parameters": {},
        "network": True,
        "writable": False,
        "memory": "256m",
        "cpus": "0.5",
        "tmpfs_size": "16M",
    }
    defaults.update(overrides)
    return ToolConfig(**defaults)


def _executor_config(**args) -> ExecutorConfig:
    """Build an ExecutorConfig with the given args."""
    return ExecutorConfig(name="exec_interactive", args=args)


class TestStartSession:
    """Tests for starting a new interactive session."""

    @patch("creel.interactive_sessions.ManagedContainer")
    @patch("creel.interactive_sessions.subprocess.Popen")
    @patch("creel.interactive_sessions._ensure_image", return_value="executor-exec-interactive:abc")
    def test_start_creates_container_with_security_flags(
        self, mock_ensure, mock_popen, mock_mc_class, manager
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        # ManagedContainer mock
        mock_container = MagicMock()
        mock_container.ping.return_value = True
        mock_container.recv.return_value = {
            "type": "started",
            "success": True,
            "session_id": "sess123",
            "command": "bash",
            "cols": 120,
            "rows": 40,
            "timeout": 300,
            "initial_output": "$ ",
        }
        mock_mc_class.return_value = mock_container

        config = _executor_config(action="start", command="bash")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))

        assert result["success"] is True
        assert result["session_id"] == "sess123"

        # Verify docker command includes security flags
        docker_cmd = mock_popen.call_args[0][0]
        assert "--read-only" in docker_cmd
        assert "--cap-drop=ALL" in docker_cmd
        assert "--security-opt=no-new-privileges" in docker_cmd
        assert any("--memory=" in arg for arg in docker_cmd)

    @patch("creel.interactive_sessions.ManagedContainer")
    @patch("creel.interactive_sessions.subprocess.Popen")
    @patch("creel.interactive_sessions._ensure_image", return_value="executor-exec-interactive:abc")
    def test_start_with_network_disabled(
        self, mock_ensure, mock_popen, mock_mc_class, manager
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        mock_container = MagicMock()
        mock_container.ping.return_value = True
        mock_container.recv.return_value = {
            "type": "started",
            "success": True,
            "session_id": "s1",
            "command": "python",
        }
        mock_mc_class.return_value = mock_container

        config = _executor_config(action="start", command="python")
        tool_cfg = _tool_config(network=False)

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is True

        docker_cmd = mock_popen.call_args[0][0]
        assert "--network=none" in docker_cmd

    def test_start_without_command_returns_error(self, manager) -> None:
        config = _executor_config(action="start")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is False
        assert "No command" in result["error"]


class TestForwardSession:
    """Tests for forwarding messages to existing sessions."""

    def test_forward_without_session_id_returns_error(self, manager) -> None:
        config = _executor_config(action="send_input")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is False
        assert "requires 'session_id'" in result["error"]

    def test_forward_unknown_session_returns_error(self, manager) -> None:
        config = _executor_config(action="send_input", session_id="nonexistent")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_forward_to_dead_container(self, manager) -> None:
        # Register a mock dead container
        mock_container = MagicMock()
        mock_container.alive = False
        mock_container.container_name = "creel-pty-dead"
        mock_container.env_file_path = "/tmp/fake.env"

        with manager._lock:
            manager._sessions["dead1"] = mock_container

        config = _executor_config(action="send_input", session_id="dead1", input="ls\n")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is False
        assert "died" in result["error"]

    def test_forward_send_input(self, manager) -> None:
        mock_container = MagicMock()
        mock_container.alive = True
        mock_container.container_name = "creel-pty-test"

        response = (
            json.dumps(
                {
                    "type": "output",
                    "success": True,
                    "session_id": "s1",
                    "output": "file1\n",
                }
            )
            + "\n"
        )
        mock_container.recv.return_value = json.loads(response.strip())

        with manager._lock:
            manager._sessions["s1"] = mock_container

        config = _executor_config(action="send_input", session_id="s1", input="ls\n")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is True
        assert result["output"] == "file1\n"

        # Verify the message sent to the container
        sent_msg = mock_container.send.call_args[0][0]
        assert sent_msg["type"] == "send_input"
        assert sent_msg["input"] == "ls\n"

    def test_close_cleans_up_session(self, manager) -> None:
        mock_container = MagicMock()
        mock_container.alive = True
        mock_container.container_name = "creel-pty-close"
        mock_container.env_file_path = "/tmp/fake.env"
        mock_container.recv.return_value = {
            "type": "closed",
            "success": True,
            "session_id": "s2",
            "exit_code": 0,
        }

        with manager._lock:
            manager._sessions["s2"] = mock_container

        config = _executor_config(action="close", session_id="s2")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is True

        # Session should be removed
        assert "s2" not in manager._sessions


class TestListSessions:
    """Tests for list_sessions action."""

    def test_list_empty(self, manager) -> None:
        config = _executor_config(action="list_sessions")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is True
        assert result["sessions"] == []

    def test_list_with_sessions(self, manager) -> None:
        mock_container = MagicMock()
        mock_container.alive = True
        mock_container.container_name = "creel-pty-list"

        with manager._lock:
            manager._sessions["s1"] = mock_container
            manager._session_started["s1"] = 0.0  # Will show large elapsed

        config = _executor_config(action="list_sessions")
        tool_cfg = _tool_config()

        result = json.loads(manager.execute(config, tool_cfg))
        assert result["success"] is True
        assert len(result["sessions"]) == 1
        assert result["sessions"][0]["session_id"] == "s1"


class TestCleanup:
    """Tests for session cleanup."""

    def test_cleanup_removes_session(self, manager) -> None:
        mock_container = MagicMock()
        mock_container.container_name = "creel-pty-cleanup"
        mock_container.env_file_path = "/tmp/fake.env"

        with manager._lock:
            manager._sessions["cleanup1"] = mock_container
            manager._session_started["cleanup1"] = 0.0

        manager._cleanup_session("cleanup1")

        assert "cleanup1" not in manager._sessions
        assert "cleanup1" not in manager._session_started
        mock_container.shutdown.assert_called_once()

    def test_cleanup_nonexistent_is_noop(self, manager) -> None:
        # Should not raise
        manager._cleanup_session("nonexistent")


class TestContainerDispatch:
    """Tests that exec_interactive routes through the container session manager."""

    @patch("creel.tools._run_interactive_via_container")
    def test_container_mode_routes_to_interactive_manager(self, mock_run) -> None:
        from creel.tools import execute_tool_call

        mock_run.return_value = json.dumps({"success": True, "session_id": "abc"})

        tool_cfg = _tool_config()
        tools_config = {"exec_interactive": tool_cfg}

        result = execute_tool_call(
            "exec_interactive",
            {"action": "start", "command": "bash"},
            tools_config,
            use_containers=True,
        )

        mock_run.assert_called_once()
        parsed = json.loads(result)
        assert parsed["success"] is True

    def test_inline_mode_does_not_route_to_container(self) -> None:
        """When use_containers=False, the inline path is used instead."""
        from unittest.mock import patch as _patch

        from creel.tools import execute_tool_call

        tool_cfg = _tool_config()
        tools_config = {"exec_interactive": tool_cfg}

        with _patch("creel.orchestrator._exec_interactive_inline") as mock_inline:
            mock_inline.return_value = json.dumps({"success": True})
            execute_tool_call(
                "exec_interactive",
                {"action": "list_sessions"},
                tools_config,
                use_containers=False,
            )
            mock_inline.assert_called_once()
