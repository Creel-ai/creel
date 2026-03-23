"""Tests for dev_session_runner.py JSON-over-stdio protocol."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch


def _make_stdin(*messages: dict) -> StringIO:
    """Create a StringIO stdin with JSON-line messages."""
    lines = [json.dumps(m) + "\n" for m in messages]
    return StringIO("".join(lines))


def _read_responses(stdout: StringIO) -> list[dict]:
    """Parse all JSON-line responses from stdout."""
    stdout.seek(0)
    responses = []
    for line in stdout:
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


class TestProtocol:
    """Test the JSON-over-stdio protocol handling."""

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_ping_pong(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        stdin = _make_stdin({"type": "ping"}, {"type": "shutdown"})
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0] == {"type": "pong"}

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_shutdown(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        stdin = _make_stdin({"type": "shutdown"})
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.shutdown.assert_called_once()
        responses = _read_responses(stdout)
        assert responses == []

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_exec_foreground(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.spawn.return_value = {
            "session_id": "echo-1",
            "command": "echo hello",
            "background": False,
            "status": "exited",
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
        }

        stdin = _make_stdin(
            {"type": "exec", "command": "echo hello", "background": False, "timeout": 30},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.spawn.assert_called_once_with(
            command="echo hello",
            background=False,
            workdir=None,
            timeout=30,
        )
        responses = _read_responses(stdout)
        assert responses[0]["type"] == "exec_result"
        assert responses[0]["session_id"] == "echo-1"
        assert responses[0]["exit_code"] == 0

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_exec_background(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.spawn.return_value = {
            "session_id": "uvicorn-1",
            "pid": 12345,
            "command": "uvicorn app:main",
            "background": True,
            "status": "running",
        }

        stdin = _make_stdin(
            {
                "type": "exec",
                "command": "uvicorn app:main",
                "background": True,
                "workdir": "/workspace",
                "timeout": 300,
            },
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.spawn.assert_called_once_with(
            command="uvicorn app:main",
            background=True,
            workdir="/workspace",
            timeout=300,
        )
        responses = _read_responses(stdout)
        assert responses[0]["type"] == "exec_result"
        assert responses[0]["session_id"] == "uvicorn-1"
        assert responses[0]["status"] == "running"

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_exec_missing_command(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        stdin = _make_stdin(
            {"type": "exec"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "command" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_exec_spawn_error(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.spawn.side_effect = ValueError("Command rejected by safety filter")

        stdin = _make_stdin(
            {"type": "exec", "command": "rm -rf /"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "safety filter" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_poll(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.poll.return_value = {
            "session_id": "uvicorn-1",
            "status": "running",
            "pid": 12345,
        }

        stdin = _make_stdin(
            {"type": "process", "session_id": "uvicorn-1", "action": "poll"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.poll.assert_called_once_with("uvicorn-1")
        responses = _read_responses(stdout)
        assert responses[0]["type"] == "process_result"
        assert responses[0]["status"] == "running"

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_log(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.log.return_value = ["[out] INFO: started", "[out] INFO: ready"]

        stdin = _make_stdin(
            {"type": "process", "session_id": "uvicorn-1", "action": "log", "limit": 10},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.log.assert_called_once_with("uvicorn-1", limit=10, offset=0)
        responses = _read_responses(stdout)
        assert responses[0]["type"] == "process_result"
        assert len(responses[0]["lines"]) == 2

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_write(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.write.return_value = {"session_id": "node-1", "written": 6}

        stdin = _make_stdin(
            {"type": "process", "session_id": "node-1", "action": "write", "data": "hello\n"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.write.assert_called_once_with("node-1", "hello\n")
        responses = _read_responses(stdout)
        assert responses[0]["type"] == "process_result"
        assert responses[0]["written"] == 6

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_write_missing_data(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        stdin = _make_stdin(
            {"type": "process", "session_id": "node-1", "action": "write"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "data" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_kill(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.kill.return_value = {
            "session_id": "uvicorn-1",
            "status": "killed",
            "exit_code": -9,
        }

        stdin = _make_stdin(
            {"type": "process", "session_id": "uvicorn-1", "action": "kill"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.kill.assert_called_once_with("uvicorn-1")
        responses = _read_responses(stdout)
        assert responses[0]["type"] == "process_result"
        assert responses[0]["status"] == "killed"

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_missing_session_id(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        stdin = _make_stdin(
            {"type": "process", "action": "poll"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "session_id" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_not_found(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.poll.side_effect = KeyError("Session not found: bad-id")

        stdin = _make_stdin(
            {"type": "process", "session_id": "bad-id", "action": "poll"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "bad-id" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_sessions_list(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.list_sessions.return_value = [
            {"session_id": "uvicorn-1", "status": "running"},
            {"session_id": "npm-1", "status": "running"},
        ]

        stdin = _make_stdin(
            {"type": "sessions"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "sessions_result"
        assert len(responses[0]["sessions"]) == 2

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_unknown_message_type(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        stdin = _make_stdin(
            {"type": "invalid"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "Unknown message type" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_unknown_process_action(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        stdin = _make_stdin(
            {"type": "process", "session_id": "test-1", "action": "invalid"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "Unknown process action" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_eof_triggers_shutdown(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        stdin = StringIO("")  # EOF immediately
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.shutdown.assert_called_once()

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_exec_default_timeout(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.spawn.return_value = {"session_id": "echo-1", "status": "exited"}

        stdin = _make_stdin(
            {"type": "exec", "command": "echo hi"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        # Default timeout is 300
        pm_instance.spawn.assert_called_once_with(
            command="echo hi",
            background=False,
            workdir=None,
            timeout=300,
        )

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_malformed_json_input(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        stdin = StringIO("not json\n")
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.shutdown.assert_called_once()

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_exec_runtime_error(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.spawn.side_effect = RuntimeError("Maximum concurrent sessions reached")

        stdin = _make_stdin(
            {"type": "exec", "command": "echo hello"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "Maximum concurrent sessions reached" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_write_runtime_error(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.write.side_effect = RuntimeError("Session not running")

        stdin = _make_stdin(
            {"type": "process", "session_id": "node-1", "action": "write", "data": "hi\n"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "Session not running" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_kill_value_error(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.kill.side_effect = ValueError("Signal not allowed")

        stdin = _make_stdin(
            {"type": "process", "session_id": "uvicorn-1", "action": "kill"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert responses[0]["type"] == "error"
        assert "Signal not allowed" in responses[0]["message"]

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_process_log_with_offset(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.log.return_value = ["[out] line 6", "[out] line 7"]

        stdin = _make_stdin(
            {
                "type": "process",
                "session_id": "uvicorn-1",
                "action": "log",
                "limit": 20,
                "offset": 5,
            },
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        pm_instance.log.assert_called_once_with("uvicorn-1", limit=20, offset=5)
        responses = _read_responses(stdout)
        assert responses[0]["type"] == "process_result"
        assert len(responses[0]["lines"]) == 2

    @patch("executors.dev_session.dev_session_runner.ProcessManager")
    def test_multi_message_sequence(self, mock_pm_cls):
        from executors.dev_session.dev_session_runner import main

        pm_instance = mock_pm_cls.return_value
        pm_instance.spawn.return_value = {
            "session_id": "echo-1",
            "command": "echo hello",
            "background": False,
            "status": "exited",
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
        }
        pm_instance.list_sessions.return_value = [
            {"session_id": "echo-1", "status": "exited"},
        ]

        stdin = _make_stdin(
            {"type": "exec", "command": "echo hello"},
            {"type": "sessions"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            main()

        responses = _read_responses(stdout)
        assert len(responses) == 2
        assert responses[0]["type"] == "exec_result"
        assert responses[0]["session_id"] == "echo-1"
        assert responses[1]["type"] == "sessions_result"
        assert len(responses[1]["sessions"]) == 1
