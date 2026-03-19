"""Tests for the exec_interactive executor."""

from __future__ import annotations

import signal
import struct
import time
from unittest.mock import patch

import pytest

from executors.exec_interactive.executor import (
    DEFAULT_COLS,
    DEFAULT_ROWS,
    DEFAULT_TIMEOUT,
    InteractiveSession,
    _build_io_summary,
    _sessions,
    _set_terminal_size,
    cleanup_timed_out_sessions,
    close_session,
    get_io_log,
    get_session_info,
    list_sessions,
    read_output,
    resize_terminal,
    send_input,
    start_session,
)


@pytest.fixture(autouse=True)
def _clear_sessions():
    """Clear the global session registry before/after each test.

    Only attempts OS-level cleanup for sessions that were created with
    real forkpty calls (pid > 0, fd > 0), not mock values.
    """
    _sessions.clear()
    yield
    _sessions.clear()


class TestStartSession:
    """Tests for start_session."""

    def test_empty_command_returns_error(self) -> None:
        result = start_session("")
        assert result["success"] is False
        assert "No command" in result["error"]

    def test_zero_timeout_returns_error(self) -> None:
        result = start_session("echo hi", timeout=0)
        assert result["success"] is False
        assert "Timeout must be positive" in result["error"]

    def test_negative_timeout_returns_error(self) -> None:
        result = start_session("echo hi", timeout=-1)
        assert result["success"] is False
        assert "Timeout must be positive" in result["error"]

    @patch("executors.exec_interactive.executor.os.forkpty")
    @patch("executors.exec_interactive.executor._set_terminal_size")
    @patch("executors.exec_interactive.executor.fcntl.fcntl")
    @patch("executors.exec_interactive.executor._read_available", return_value=b"$ ")
    def test_successful_start(self, mock_read, mock_fcntl, mock_setsize, mock_forkpty) -> None:
        mock_forkpty.return_value = (42, 5)  # pid=42, fd=5

        result = start_session("bash", timeout=60, cols=80, rows=24)

        assert result["success"] is True
        assert "session_id" in result
        assert result["command"] == "bash"
        assert result["cols"] == 80
        assert result["rows"] == 24
        assert result["timeout"] == 60
        assert result["initial_output"] == "$ "

        # Verify session was registered
        assert result["session_id"] in _sessions
        session = _sessions[result["session_id"]]
        assert session.pid == 42
        assert session.fd == 5

        mock_setsize.assert_called_once_with(5, 80, 24)

    @patch("executors.exec_interactive.executor.os.forkpty")
    def test_forkpty_failure(self, mock_forkpty) -> None:
        mock_forkpty.side_effect = OSError("PTY allocation failed")

        result = start_session("bash")

        assert result["success"] is False
        assert "Failed to allocate PTY" in result["error"]

    @patch("executors.exec_interactive.executor.os.forkpty")
    @patch("executors.exec_interactive.executor._set_terminal_size")
    @patch("executors.exec_interactive.executor.fcntl.fcntl")
    @patch("executors.exec_interactive.executor._read_available", return_value=b"")
    def test_default_terminal_size(self, mock_read, mock_fcntl, mock_setsize, mock_forkpty) -> None:
        mock_forkpty.return_value = (42, 5)

        result = start_session("bash")

        assert result["cols"] == DEFAULT_COLS
        assert result["rows"] == DEFAULT_ROWS
        assert result["timeout"] == DEFAULT_TIMEOUT
        mock_setsize.assert_called_once_with(5, DEFAULT_COLS, DEFAULT_ROWS)


class TestSendInput:
    """Tests for send_input."""

    def test_unknown_session(self) -> None:
        result = send_input("nonexistent", "ls\n")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_closed_session(self) -> None:
        session = InteractiveSession(
            session_id="closed1",
            pid=0,
            fd=0,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
            _closed=True,
        )
        _sessions["closed1"] = session

        result = send_input("closed1", "ls\n")
        assert result["success"] is False
        assert "already closed" in result["error"]

    def test_timed_out_session(self) -> None:
        session = InteractiveSession(
            session_id="timeout1",
            pid=0,
            fd=0,
            command="bash",
            cols=80,
            rows=24,
            timeout=1,
            started_at=time.monotonic() - 10,  # Started 10 seconds ago with 1s timeout
        )
        _sessions["timeout1"] = session

        with patch("executors.exec_interactive.executor._force_close") as mock_close:
            mock_close.return_value = {"success": True}
            result = send_input("timeout1", "ls\n")
            assert result["success"] is False
            assert "timed out" in result["error"]
            mock_close.assert_called_once()

    @patch("executors.exec_interactive.executor.os.write")
    @patch("executors.exec_interactive.executor._read_available", return_value=b"file1\nfile2\n")
    def test_send_input_success(self, mock_read, mock_write) -> None:
        session = InteractiveSession(
            session_id="active1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["active1"] = session

        result = send_input("active1", "ls\n")

        assert result["success"] is True
        assert result["output"] == "file1\nfile2\n"
        mock_write.assert_called_once_with(5, b"ls\n")

        # Check I/O log
        assert len(session.io_log) == 2
        assert session.io_log[0]["direction"] == "input"
        assert session.io_log[0]["data"] == "ls\n"
        assert session.io_log[1]["direction"] == "output"

    @patch("executors.exec_interactive.executor.os.write")
    def test_send_input_eio_closes_session(self, mock_write) -> None:
        import errno as errno_module

        mock_write.side_effect = OSError(errno_module.EIO, "Input/output error")

        session = InteractiveSession(
            session_id="eio1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["eio1"] = session

        with patch("executors.exec_interactive.executor._force_close") as mock_close:
            mock_close.return_value = {"success": True}
            result = send_input("eio1", "ls\n")
            assert result["success"] is False
            assert "has ended" in result["error"]


class TestReadOutput:
    """Tests for read_output."""

    def test_unknown_session(self) -> None:
        result = read_output("nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_closed_session(self) -> None:
        session = InteractiveSession(
            session_id="closed2",
            pid=0,
            fd=0,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
            _closed=True,
        )
        _sessions["closed2"] = session

        result = read_output("closed2")
        assert result["success"] is False
        assert "already closed" in result["error"]

    @patch("executors.exec_interactive.executor._read_available", return_value=b"hello\n")
    def test_read_output_success(self, mock_read) -> None:
        session = InteractiveSession(
            session_id="read1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["read1"] = session

        result = read_output("read1", timeout=5.0)

        assert result["success"] is True
        assert result["output"] == "hello\n"
        assert "elapsed" in result
        assert "remaining" in result

    @patch("executors.exec_interactive.executor._read_available", return_value=b"")
    def test_read_output_caps_timeout_to_remaining(self, mock_read) -> None:
        session = InteractiveSession(
            session_id="cap1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=5,
            started_at=time.monotonic() - 3,  # 3 seconds elapsed, 2 remaining
        )
        _sessions["cap1"] = session

        read_output("cap1", timeout=10.0)

        # The effective timeout should be capped to ~2s (remaining time)
        actual_timeout = mock_read.call_args[1]["timeout"]
        assert actual_timeout <= 2.1  # Allow small float margin


class TestResizeTerminal:
    """Tests for resize_terminal."""

    def test_unknown_session(self) -> None:
        result = resize_terminal("nonexistent", 80, 24)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_closed_session(self) -> None:
        session = InteractiveSession(
            session_id="closed3",
            pid=0,
            fd=0,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
            _closed=True,
        )
        _sessions["closed3"] = session

        result = resize_terminal("closed3", 100, 50)
        assert result["success"] is False
        assert "already closed" in result["error"]

    @patch("executors.exec_interactive.executor._set_terminal_size")
    def test_resize_success(self, mock_setsize) -> None:
        session = InteractiveSession(
            session_id="resize1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["resize1"] = session

        result = resize_terminal("resize1", 132, 50)

        assert result["success"] is True
        assert result["cols"] == 132
        assert result["rows"] == 50
        assert session.cols == 132
        assert session.rows == 50
        mock_setsize.assert_called_once_with(5, 132, 50)

    @patch("executors.exec_interactive.executor._set_terminal_size")
    def test_resize_oserror(self, mock_setsize) -> None:
        mock_setsize.side_effect = OSError("ioctl failed")

        session = InteractiveSession(
            session_id="resize_err",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["resize_err"] = session

        result = resize_terminal("resize_err", 132, 50)
        assert result["success"] is False
        assert "Failed to resize" in result["error"]


class TestCloseSession:
    """Tests for close_session."""

    def test_unknown_session(self) -> None:
        result = close_session("nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_already_closed(self) -> None:
        session = InteractiveSession(
            session_id="closed4",
            pid=0,
            fd=0,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
            _closed=True,
        )
        _sessions["closed4"] = session

        result = close_session("closed4")
        assert result["success"] is True
        assert result["already_closed"] is True
        assert "io_summary" in result

    @patch("executors.exec_interactive.executor._force_close")
    def test_close_delegates_to_force_close(self, mock_force_close) -> None:
        mock_force_close.return_value = {
            "success": True,
            "session_id": "active2",
            "exit_code": 0,
        }

        session = InteractiveSession(
            session_id="active2",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["active2"] = session

        result = close_session("active2")
        assert result["success"] is True
        mock_force_close.assert_called_once_with(session)


class TestGetSessionInfo:
    """Tests for get_session_info."""

    def test_unknown_session(self) -> None:
        result = get_session_info("nonexistent")
        assert result["success"] is False

    def test_session_info(self) -> None:
        session = InteractiveSession(
            session_id="info1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        session.io_log.append({"direction": "output", "data": "$ ", "ts": time.time()})
        _sessions["info1"] = session

        result = get_session_info("info1")

        assert result["success"] is True
        assert result["command"] == "bash"
        assert result["cols"] == 80
        assert result["rows"] == 24
        assert result["timeout"] == 300
        assert result["closed"] is False
        assert result["io_events"] == 1
        assert "elapsed" in result
        assert "remaining" in result


class TestListSessions:
    """Tests for list_sessions."""

    def test_empty(self) -> None:
        result = list_sessions()
        assert result["success"] is True
        assert result["sessions"] == []

    def test_with_sessions(self) -> None:
        for i in range(3):
            session = InteractiveSession(
                session_id=f"list{i}",
                pid=42 + i,
                fd=5 + i,
                command=f"cmd{i}",
                cols=80,
                rows=24,
                timeout=300,
                started_at=time.monotonic(),
            )
            _sessions[f"list{i}"] = session

        result = list_sessions()
        assert result["success"] is True
        assert len(result["sessions"]) == 3


class TestGetIoLog:
    """Tests for get_io_log."""

    def test_unknown_session(self) -> None:
        result = get_io_log("nonexistent")
        assert result["success"] is False

    def test_io_log(self) -> None:
        session = InteractiveSession(
            session_id="log1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        session.io_log.append({"direction": "input", "data": "ls\n", "ts": time.time()})
        session.io_log.append({"direction": "output", "data": "file1\n", "ts": time.time()})
        _sessions["log1"] = session

        result = get_io_log("log1")

        assert result["success"] is True
        assert len(result["io_log"]) == 2
        assert result["command"] == "bash"


class TestBuildIoSummary:
    """Tests for _build_io_summary."""

    def test_summary(self) -> None:
        session = InteractiveSession(
            session_id="sum1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        session.io_log = [
            {"direction": "input", "data": "ls\n", "ts": time.time()},
            {"direction": "output", "data": "file1\nfile2\n", "ts": time.time()},
            {"direction": "input", "data": "pwd\n", "ts": time.time()},
            {"direction": "output", "data": "/home\n", "ts": time.time()},
        ]

        summary = _build_io_summary(session)

        assert summary["input_events"] == 2
        assert summary["output_events"] == 2
        assert summary["total_input_bytes"] == 7  # "ls\n" + "pwd\n"
        assert summary["total_output_bytes"] == 18  # "file1\nfile2\n" + "/home\n"
        assert summary["total_events"] == 4


class TestCleanupTimedOutSessions:
    """Tests for cleanup_timed_out_sessions."""

    def test_no_sessions(self) -> None:
        cleaned = cleanup_timed_out_sessions()
        assert cleaned == []

    def test_cleans_timed_out(self) -> None:
        # Active session (not timed out)
        active = InteractiveSession(
            session_id="active",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["active"] = active

        # Timed out session
        timed_out = InteractiveSession(
            session_id="timed_out",
            pid=43,
            fd=6,
            command="bash",
            cols=80,
            rows=24,
            timeout=1,
            started_at=time.monotonic() - 10,
        )
        _sessions["timed_out"] = timed_out

        with patch("executors.exec_interactive.executor._force_close") as mock_close:
            mock_close.return_value = {"success": True}
            cleaned = cleanup_timed_out_sessions()

        assert "timed_out" in cleaned
        assert "active" not in cleaned
        mock_close.assert_called_once_with(timed_out)


class TestSetTerminalSize:
    """Tests for _set_terminal_size."""

    @patch("executors.exec_interactive.executor.fcntl.ioctl")
    def test_set_terminal_size(self, mock_ioctl) -> None:
        _set_terminal_size(5, 120, 40)

        mock_ioctl.assert_called_once()
        args = mock_ioctl.call_args
        assert args[0][0] == 5  # fd
        # Verify the winsize struct
        winsize = args[0][2]
        rows, cols, _, _ = struct.unpack("HHHH", winsize)
        assert rows == 40
        assert cols == 120


class TestInteractiveSessionProperties:
    """Tests for InteractiveSession dataclass properties."""

    def test_elapsed(self) -> None:
        session = InteractiveSession(
            session_id="prop1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic() - 5.0,
        )
        assert session.elapsed >= 5.0

    def test_timed_out_false(self) -> None:
        session = InteractiveSession(
            session_id="prop2",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        assert session.timed_out is False

    def test_timed_out_true(self) -> None:
        session = InteractiveSession(
            session_id="prop3",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=1,
            started_at=time.monotonic() - 10,
        )
        assert session.timed_out is True

    def test_closed_default_false(self) -> None:
        session = InteractiveSession(
            session_id="prop4",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        assert session.closed is False


class TestForceClose:
    """Tests for _force_close (via close_session)."""

    @patch("executors.exec_interactive.executor.os.close")
    @patch("executors.exec_interactive.executor.os.waitpid", return_value=(42, 0))
    @patch("executors.exec_interactive.executor.os.killpg")
    def test_graceful_sigterm(self, mock_killpg, mock_waitpid, mock_close) -> None:
        session = InteractiveSession(
            session_id="force1",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["force1"] = session

        result = close_session("force1")

        assert result["success"] is True
        assert session.closed is True
        mock_killpg.assert_called_with(42, signal.SIGTERM)
        mock_close.assert_called_once_with(5)

    @patch("executors.exec_interactive.executor.os.close")
    @patch("executors.exec_interactive.executor.os.waitpid")
    @patch("executors.exec_interactive.executor.os.killpg")
    def test_force_sigkill_on_stubborn_process(self, mock_killpg, mock_waitpid, mock_close) -> None:
        # First waitpid calls return 0 (still running), then last returns after SIGKILL
        mock_waitpid.side_effect = [
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
            (42, 0),  # After SIGKILL
        ]

        session = InteractiveSession(
            session_id="force2",
            pid=42,
            fd=5,
            command="bash",
            cols=80,
            rows=24,
            timeout=300,
            started_at=time.monotonic(),
        )
        _sessions["force2"] = session

        result = close_session("force2")

        assert result["success"] is True
        # Should have sent SIGTERM then SIGKILL to process group
        assert mock_killpg.call_count == 2
        mock_killpg.assert_any_call(42, signal.SIGTERM)
        mock_killpg.assert_any_call(42, signal.SIGKILL)


class TestOrchestratorDispatch:
    """Tests for exec_interactive dispatch through the orchestrator."""

    def test_unknown_action_raises(self) -> None:
        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        config = ExecutorConfig(name="exec_interactive", args={"action": "invalid"})
        with pytest.raises(ValueError, match="unknown action"):
            _exec_interactive_inline(config)

    def test_start_requires_command(self) -> None:
        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        config = ExecutorConfig(name="exec_interactive", args={"action": "start"})
        with pytest.raises(ValueError, match="requires a 'command'"):
            _exec_interactive_inline(config)

    def test_send_input_requires_session_id(self) -> None:
        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        config = ExecutorConfig(name="exec_interactive", args={"action": "send_input"})
        with pytest.raises(ValueError, match="requires 'session_id'"):
            _exec_interactive_inline(config)

    def test_read_output_requires_session_id(self) -> None:
        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        config = ExecutorConfig(name="exec_interactive", args={"action": "read_output"})
        with pytest.raises(ValueError, match="requires 'session_id'"):
            _exec_interactive_inline(config)

    def test_resize_requires_session_id(self) -> None:
        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        config = ExecutorConfig(name="exec_interactive", args={"action": "resize"})
        with pytest.raises(ValueError, match="requires 'session_id'"):
            _exec_interactive_inline(config)

    def test_close_requires_session_id(self) -> None:
        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        config = ExecutorConfig(name="exec_interactive", args={"action": "close"})
        with pytest.raises(ValueError, match="requires 'session_id'"):
            _exec_interactive_inline(config)

    def test_info_requires_session_id(self) -> None:
        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        config = ExecutorConfig(name="exec_interactive", args={"action": "info"})
        with pytest.raises(ValueError, match="requires 'session_id'"):
            _exec_interactive_inline(config)

    @patch("executors.exec_interactive.executor.list_sessions")
    def test_list_sessions_dispatches(self, mock_list) -> None:
        import json

        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        mock_list.return_value = {"success": True, "sessions": []}
        config = ExecutorConfig(name="exec_interactive", args={"action": "list_sessions"})
        result = json.loads(_exec_interactive_inline(config))
        assert result["success"] is True

    @patch("executors.exec_interactive.executor.start_session")
    def test_start_dispatches_with_args(self, mock_start) -> None:
        import json

        from creel.models import ExecutorConfig
        from creel.orchestrator import _exec_interactive_inline

        mock_start.return_value = {"success": True, "session_id": "abc123"}
        config = ExecutorConfig(
            name="exec_interactive",
            args={
                "action": "start",
                "command": "bash",
                "timeout": "120",
                "cols": "80",
                "rows": "24",
            },
        )
        result = json.loads(_exec_interactive_inline(config))
        assert result["success"] is True
        mock_start.assert_called_once_with("bash", timeout=120, cols=80, rows=24)


class TestAuditLogMethods:
    """Tests for the new audit log methods."""

    def test_log_interactive_io(self, tmp_path) -> None:
        import json

        from guardian.audit import AuditLogger

        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)

        logger.log_interactive_io(
            session_id="abc123",
            tool_name="exec_interactive",
            direction="input",
            data_length=5,
            data_hash="abcdef1234567890",
        )

        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["event"] == "interactive_io"
        assert entries[0]["session_id"] == "abc123"
        assert entries[0]["direction"] == "input"
        assert entries[0]["data_length"] == 5
        assert entries[0]["data_hash"] == "abcdef1234567890"

    def test_log_interactive_session_start(self, tmp_path) -> None:
        import json

        from guardian.audit import AuditLogger

        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)

        logger.log_interactive_session(
            session_id="abc123",
            tool_name="exec_interactive",
            action="start",
            command_hash="deadbeef12345678",
        )

        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["event"] == "interactive_session"
        assert entries[0]["action"] == "start"
        assert entries[0]["command_hash"] == "deadbeef12345678"

    def test_log_interactive_session_close(self, tmp_path) -> None:
        import json

        from guardian.audit import AuditLogger

        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)

        logger.log_interactive_session(
            session_id="abc123",
            tool_name="exec_interactive",
            action="close",
            exit_code=0,
            duration_s=45.3,
            io_summary={"input_events": 3, "output_events": 5},
        )

        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["exit_code"] == 0
        assert entries[0]["duration_s"] == 45.3
        assert entries[0]["io_summary"]["input_events"] == 3


class TestPolicyRules:
    """Tests that the policy file includes exec_interactive rules."""

    def test_exec_interactive_in_review(self) -> None:
        import yaml

        with open("policies/default.yaml") as f:
            policy = yaml.safe_load(f)

        assert "exec_interactive" in policy["review"]

    def test_exec_interactive_deny_patterns(self) -> None:
        import yaml

        with open("policies/default.yaml") as f:
            policy = yaml.safe_load(f)

        exec_interactive_deny = [r for r in policy["deny_when"] if r["tool"] == "exec_interactive"]
        # Should have at least the key dangerous patterns
        patterns = {r["pattern"] for r in exec_interactive_deny}
        assert "*rm -rf*" in patterns
        assert "*rm -fr*" in patterns
        assert "*>/dev/tcp/*" in patterns
        assert "*| bash*" in patterns
        assert "*:(){ :|:& };:*" in patterns

    def test_bash_i_deny_pattern(self) -> None:
        import yaml

        with open("policies/default.yaml") as f:
            policy = yaml.safe_load(f)

        exec_interactive_deny = [r for r in policy["deny_when"] if r["tool"] == "exec_interactive"]
        patterns = {r["pattern"] for r in exec_interactive_deny}
        assert "*bash -i*" in patterns, "exec_interactive must deny 'bash -i' (reverse shell)"

    def test_exec_interactive_review_patterns(self) -> None:
        import yaml

        with open("policies/default.yaml") as f:
            policy = yaml.safe_load(f)

        exec_interactive_review = [
            r for r in policy["review_when"] if r["tool"] == "exec_interactive"
        ]
        patterns = {r["pattern"] for r in exec_interactive_review}
        assert "*sudo*" in patterns
        assert "*env*KEY*" in patterns


class TestContainerDispatch:
    """Tests that exec_interactive routes through containers when enabled."""

    @patch("creel.tools._run_interactive_via_container")
    def test_container_mode_routes_to_session_manager(self, mock_run) -> None:
        import json

        from creel.models import ToolConfig
        from creel.tools import execute_tool_call

        mock_run.return_value = json.dumps({"success": True, "session_id": "test123"})

        tool_cfg = ToolConfig(
            executor="exec_interactive",
            description="test",
            parameters={},
            network=True,
        )
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
