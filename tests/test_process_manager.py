"""Tests for the ProcessManager class."""

from __future__ import annotations

import time
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from bridge.process_manager import ProcessManager, ProcessSession


@pytest.fixture
def pm():
    """Create a ProcessManager for testing."""
    manager = ProcessManager(max_sessions=10, max_age_hours=4)
    yield manager
    manager.shutdown()


class TestSessionIdGeneration:
    """Test session ID generation from command names."""

    def test_simple_command(self, pm):
        sid = pm._generate_session_id("echo hello")
        assert sid == "echo-1"

    def test_path_command(self, pm):
        sid = pm._generate_session_id("/usr/bin/python3 script.py")
        assert sid == "python3-1"

    def test_script_extension_stripped(self, pm):
        sid = pm._generate_session_id("server.py")
        assert sid == "server-1"

    def test_counter_increments(self, pm):
        sid1 = pm._generate_session_id("npm start")
        sid2 = pm._generate_session_id("npm test")
        assert sid1 == "npm-1"
        assert sid2 == "npm-2"

    def test_empty_command_fallback(self, pm):
        sid = pm._generate_session_id("")
        assert sid == "cmd-1"

    def test_special_chars_sanitized(self, pm):
        sid = pm._generate_session_id("some/weird@cmd!")
        # basename is "weird@cmd!", sanitized to "weirdcmd"
        assert sid == "weirdcmd-1"

    def test_different_commands_independent_counters(self, pm):
        pm._generate_session_id("uvicorn app")
        pm._generate_session_id("npm start")
        sid = pm._generate_session_id("uvicorn reload")
        assert sid == "uvicorn-2"


class TestSpawnForeground:
    """Test foreground command execution."""

    def test_foreground_success(self, pm):
        result = pm.spawn("echo hello", background=False, timeout=10)
        assert result["background"] is False
        assert result["status"] == "exited"
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

    def test_foreground_failure(self, pm):
        result = pm.spawn("false", background=False, timeout=10)
        assert result["status"] == "exited"
        assert result["exit_code"] != 0

    def test_foreground_timeout(self, pm):
        result = pm.spawn("sleep 60", background=False, timeout=1)
        assert result["status"] == "timeout"
        assert "timed out" in result.get("error", "")

    def test_foreground_with_workdir(self, pm, tmp_path):
        result = pm.spawn("pwd", workdir=str(tmp_path), background=False, timeout=10)
        assert result["exit_code"] == 0
        assert str(tmp_path) in result["stdout"]


class TestSpawnBackground:
    """Test background process spawning."""

    def test_background_spawn(self, pm):
        result = pm.spawn("sleep 30", background=True, timeout=60)
        assert result["background"] is True
        assert result["status"] == "running"
        assert "session_id" in result
        assert "pid" in result

    def test_background_output_capture(self, pm):
        result = pm.spawn(
            'echo "line1" && echo "line2" && sleep 0.5',
            background=True,
            timeout=10,
        )
        sid = result["session_id"]
        # Give reader threads time to capture output
        time.sleep(1)
        lines = pm.log(sid)
        stdout_lines = [line for line in lines if line.startswith("[out]")]
        assert len(stdout_lines) >= 2
        assert "[out] line1" in stdout_lines
        assert "[out] line2" in stdout_lines

    def test_background_stderr_capture(self, pm):
        result = pm.spawn(
            'echo "err1" >&2 && sleep 0.5',
            background=True,
            timeout=10,
        )
        sid = result["session_id"]
        time.sleep(1)
        lines = pm.log(sid)
        err_lines = [line for line in lines if line.startswith("[err]")]
        assert len(err_lines) >= 1
        assert "[err] err1" in err_lines

    def test_background_process_exits(self, pm):
        result = pm.spawn("echo done", background=True, timeout=10)
        sid = result["session_id"]
        time.sleep(2)
        status = pm.poll(sid)
        assert status["status"] == "exited"
        assert status["exit_code"] == 0


class TestPoll:
    """Test session polling."""

    def test_poll_running(self, pm):
        result = pm.spawn("sleep 30", background=True, timeout=60)
        sid = result["session_id"]
        status = pm.poll(sid)
        assert status["status"] == "running"
        assert status["pid"] == result["pid"]

    def test_poll_exited(self, pm):
        result = pm.spawn("true", background=True, timeout=10)
        sid = result["session_id"]
        time.sleep(0.5)
        status = pm.poll(sid)
        assert status["status"] == "exited"
        assert status["exit_code"] == 0

    def test_poll_nonexistent(self, pm):
        with pytest.raises(KeyError, match="Session not found"):
            pm.poll("nonexistent-1")


class TestLog:
    """Test log retrieval."""

    def test_log_with_limit(self, pm):
        result = pm.spawn(
            'for i in $(seq 1 20); do echo "line$i"; done && sleep 0.5',
            background=True,
            timeout=10,
        )
        sid = result["session_id"]
        time.sleep(1)
        lines = pm.log(sid, limit=5)
        assert len(lines) == 5

    def test_log_with_offset(self, pm):
        result = pm.spawn(
            'for i in $(seq 1 10); do echo "line$i"; done && sleep 0.5',
            background=True,
            timeout=10,
        )
        sid = result["session_id"]
        time.sleep(1)
        all_lines = pm.log(sid, limit=100)
        offset_lines = pm.log(sid, limit=100, offset=3)
        assert len(offset_lines) == len(all_lines) - 3

    def test_log_nonexistent_session(self, pm):
        with pytest.raises(KeyError):
            pm.log("nonexistent-1")


class TestWrite:
    """Test writing to process stdin."""

    def test_write_to_running_process(self, pm):
        result = pm.spawn("cat", background=True, timeout=10)
        sid = result["session_id"]
        time.sleep(0.3)
        write_result = pm.write(sid, "hello world")
        assert write_result["session_id"] == sid
        assert write_result["written"] > 0
        time.sleep(0.5)
        lines = pm.log(sid)
        assert any("hello world" in line for line in lines)

    def test_write_to_exited_process(self, pm):
        result = pm.spawn("true", background=True, timeout=10)
        sid = result["session_id"]
        time.sleep(0.5)
        with pytest.raises(RuntimeError, match="not running"):
            pm.write(sid, "data")

    def test_write_appends_newline(self, pm):
        result = pm.spawn("cat", background=True, timeout=10)
        sid = result["session_id"]
        time.sleep(0.3)
        write_result = pm.write(sid, "no newline")
        # data + newline = 11 chars
        assert write_result["written"] == len("no newline\n")


class TestKill:
    """Test killing processes."""

    def test_kill_running_process(self, pm):
        result = pm.spawn("sleep 300", background=True, timeout=600)
        sid = result["session_id"]
        kill_result = pm.kill(sid)
        assert kill_result["status"] == "killed"

    def test_kill_already_exited(self, pm):
        result = pm.spawn("true", background=True, timeout=10)
        sid = result["session_id"]
        time.sleep(0.5)
        kill_result = pm.kill(sid)
        assert kill_result["message"] == "Process already stopped"

    def test_kill_nonexistent(self, pm):
        with pytest.raises(KeyError):
            pm.kill("nonexistent-1")


class TestListSessions:
    """Test listing sessions."""

    def test_list_empty(self, pm):
        sessions = pm.list_sessions()
        assert sessions == []

    def test_list_multiple(self, pm):
        pm.spawn("sleep 30", background=True, timeout=60)
        pm.spawn("sleep 30", background=True, timeout=60)
        sessions = pm.list_sessions()
        assert len(sessions) == 2


class TestMaxSessions:
    """Test max concurrent sessions limit."""

    def test_max_sessions_enforced(self):
        pm = ProcessManager(max_sessions=2)
        try:
            pm.spawn("sleep 30", background=True, timeout=60)
            pm.spawn("sleep 30", background=True, timeout=60)
            with pytest.raises(RuntimeError, match="Maximum concurrent sessions"):
                pm.spawn("sleep 30", background=True, timeout=60)
        finally:
            pm.shutdown()


class TestRingBuffer:
    """Test ring buffer behavior for output capture."""

    def test_buffer_respects_maxlen(self):
        pm = ProcessManager(buffer_lines=5)
        try:
            result = pm.spawn(
                'for i in $(seq 1 20); do echo "line$i"; done && sleep 0.5',
                background=True,
                timeout=10,
            )
            sid = result["session_id"]
            time.sleep(1)
            lines = pm.log(sid, limit=100)
            # Combined buffer has maxlen=5, so we should see at most 5 lines
            assert len(lines) <= 5
            # Should contain the last lines, not the first
            if lines:
                assert "line20" in lines[-1] or "line19" in lines[-1] or "line18" in lines[-1]
        finally:
            pm.shutdown()


class TestCleanupStale:
    """Test stale session cleanup."""

    def test_cleanup_removes_old_sessions(self, pm):
        result = pm.spawn("sleep 300", background=True, timeout=600)
        sid = result["session_id"]
        # Force the session to appear old
        session = pm._get_session(sid)
        from datetime import datetime, timedelta

        session.started_at = datetime.now(tz=UTC) - timedelta(hours=5)
        cleaned = pm.cleanup_stale(max_age_hours=4)
        assert cleaned == 1
        with pytest.raises(KeyError):
            pm.poll(sid)

    def test_cleanup_keeps_recent(self, pm):
        pm.spawn("sleep 30", background=True, timeout=60)
        cleaned = pm.cleanup_stale(max_age_hours=4)
        assert cleaned == 0
        assert len(pm.list_sessions()) == 1


class TestConcurrentSessions:
    """Test concurrent session management."""

    def test_multiple_concurrent_sessions(self, pm):
        results = []
        for i in range(5):
            r = pm.spawn(f"echo session{i} && sleep 1", background=True, timeout=10)
            results.append(r)

        # All should have unique session IDs
        sids = [r["session_id"] for r in results]
        assert len(set(sids)) == 5

        # All should be listed
        sessions = pm.list_sessions()
        assert len(sessions) == 5


class TestProcessSessionDataclass:
    """Test ProcessSession dataclass serialization."""

    def test_to_dict(self):
        proc = MagicMock()
        proc.pid = 12345
        session = ProcessSession(
            session_id="test-1",
            pid=12345,
            command="echo hello",
            workdir="/tmp",
            process=proc,
        )
        d = session.to_dict()
        assert d["session_id"] == "test-1"
        assert d["pid"] == 12345
        assert d["command"] == "echo hello"
        assert d["workdir"] == "/tmp"
        assert d["status"] == "running"
        assert d["exit_code"] is None
        assert "started_at" in d
        assert d["last_output_at"] is None
        assert d["output_lines"] == 0

    def test_to_dict_with_output(self):
        from datetime import datetime

        proc = MagicMock()
        proc.pid = 12345
        now = datetime.now(tz=UTC)
        session = ProcessSession(
            session_id="test-1",
            pid=12345,
            command="echo hello",
            workdir="/tmp",
            process=proc,
            last_output_at=now,
            output_lines=42,
        )
        d = session.to_dict()
        assert d["last_output_at"] == now.isoformat()
        assert d["output_lines"] == 42


class TestValidation:
    """Test input validation."""

    def test_empty_command_rejected(self, pm):
        with pytest.raises(ValueError, match="must not be empty"):
            pm.spawn("", background=True)

    def test_whitespace_command_rejected(self, pm):
        with pytest.raises(ValueError, match="must not be empty"):
            pm.spawn("   ", background=True)

    def test_relative_workdir_rejected(self, pm):
        with pytest.raises(ValueError, match="absolute path"):
            pm.spawn("echo hi", workdir="relative/path", background=True)

    def test_nonexistent_workdir_rejected(self, pm):
        with pytest.raises(ValueError, match="does not exist"):
            pm.spawn("echo hi", workdir="/nonexistent/path/xyz", background=True)


class TestAllowedWorkdirs:
    """Test allowed_workdirs validation."""

    def test_allowed_workdir_accepted(self, tmp_path):
        pm = ProcessManager(allowed_workdirs=[str(tmp_path)])
        try:
            result = pm.spawn("echo hi", workdir=str(tmp_path), background=False, timeout=5)
            assert result["exit_code"] == 0
        finally:
            pm.shutdown()

    def test_disallowed_workdir_rejected(self, tmp_path):
        pm = ProcessManager(allowed_workdirs=["/some/other/dir"])
        try:
            with pytest.raises(ValueError, match="not under any allowed prefix"):
                pm.spawn("echo hi", workdir=str(tmp_path), background=False, timeout=5)
        finally:
            pm.shutdown()

    def test_empty_allowed_allows_all(self, tmp_path):
        pm = ProcessManager(allowed_workdirs=None)
        try:
            result = pm.spawn("echo hi", workdir=str(tmp_path), background=False, timeout=5)
            assert result["exit_code"] == 0
        finally:
            pm.shutdown()

    def test_subdirectory_allowed(self, tmp_path):
        subdir = tmp_path / "sub" / "dir"
        subdir.mkdir(parents=True)
        pm = ProcessManager(allowed_workdirs=[str(tmp_path)])
        try:
            result = pm.spawn("echo hi", workdir=str(subdir), background=False, timeout=5)
            assert result["exit_code"] == 0
        finally:
            pm.shutdown()

    def test_symlink_resolved(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        pm = ProcessManager(allowed_workdirs=[str(real_dir)])
        try:
            result = pm.spawn("echo hi", workdir=str(link), background=False, timeout=5)
            assert result["exit_code"] == 0
        finally:
            pm.shutdown()

    def test_no_workdir_skips_validation(self, tmp_path):
        pm = ProcessManager(allowed_workdirs=["/some/other/dir"])
        try:
            result = pm.spawn("echo hi", background=False, timeout=5)
            assert result["exit_code"] == 0
        finally:
            pm.shutdown()


class TestPeriodicCleanup:
    """Test periodic cleanup thread."""

    def test_periodic_cleanup_fires(self):
        pm = ProcessManager(max_age_hours=0, cleanup_interval=1)
        try:
            result = pm.spawn("sleep 300", background=True, timeout=600)
            sid = result["session_id"]
            # Force the session to appear old
            session = pm._get_session(sid)
            from datetime import datetime, timedelta

            session.started_at = datetime.now(tz=UTC) - timedelta(hours=1)
            # Wait for the cleanup thread to fire (interval=1s)
            time.sleep(2.5)
            with pytest.raises(KeyError):
                pm.poll(sid)
        finally:
            pm.shutdown()

    def test_cleanup_thread_stops_on_shutdown(self):
        pm = ProcessManager(cleanup_interval=1)
        assert pm._cleanup_thread.is_alive()
        pm.shutdown()
        assert not pm._cleanup_thread.is_alive()


class TestLastOutputAt:
    """Test last_output_at tracking."""

    def test_last_output_at_updates(self, pm):
        result = pm.spawn(
            'echo "hello" && sleep 0.5',
            background=True,
            timeout=10,
        )
        sid = result["session_id"]
        time.sleep(1)
        session = pm._get_session(sid)
        assert session.last_output_at is not None

    def test_last_output_at_in_poll(self, pm):
        result = pm.spawn(
            'echo "hello" && sleep 0.5',
            background=True,
            timeout=10,
        )
        sid = result["session_id"]
        time.sleep(1)
        status = pm.poll(sid)
        assert status["last_output_at"] is not None


class TestOutputLines:
    """Test output_lines counter."""

    def test_output_lines_counts_all(self, pm):
        result = pm.spawn(
            'for i in $(seq 1 10); do echo "line$i"; done && sleep 0.5',
            background=True,
            timeout=10,
        )
        sid = result["session_id"]
        time.sleep(1)
        session = pm._get_session(sid)
        assert session.output_lines >= 10

    def test_output_lines_in_poll(self, pm):
        result = pm.spawn(
            'for i in $(seq 1 5); do echo "line$i"; done && sleep 0.5',
            background=True,
            timeout=10,
        )
        sid = result["session_id"]
        time.sleep(1)
        status = pm.poll(sid)
        assert status["output_lines"] >= 5

    def test_output_lines_exceeds_buffer(self):
        pm = ProcessManager(buffer_lines=3)
        try:
            result = pm.spawn(
                'for i in $(seq 1 10); do echo "line$i"; done && sleep 0.5',
                background=True,
                timeout=10,
            )
            sid = result["session_id"]
            time.sleep(1)
            session = pm._get_session(sid)
            # output_lines counts all, buffer only keeps 3
            assert session.output_lines >= 10
            assert len(session.stdout_buffer) <= 3
        finally:
            pm.shutdown()
