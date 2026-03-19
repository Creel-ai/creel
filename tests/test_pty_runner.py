"""Tests for the container-side PTY runner (pty_runner.py).

Tests the JSON-over-stdio protocol by mocking stdin/stdout and the
underlying executor functions.
"""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

from executors.exec_interactive.pty_runner import main


def _make_stdin(*messages: dict) -> StringIO:
    """Build a StringIO that produces the given JSON lines."""
    lines = [json.dumps(m) + "\n" for m in messages]
    return StringIO("".join(lines))


class TestPing:
    """Test ping/pong protocol."""

    def test_ping_returns_pong(self) -> None:
        stdin = _make_stdin({"type": "ping"}, {"type": "shutdown"})
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        lines = stdout.getvalue().strip().split("\n")
        assert len(lines) == 1
        msg = json.loads(lines[0])
        assert msg["type"] == "pong"


class TestShutdown:
    """Test shutdown message."""

    def test_shutdown_exits(self) -> None:
        stdin = _make_stdin({"type": "shutdown"})
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        # No output expected
        assert stdout.getvalue().strip() == ""


class TestStart:
    """Test start message routing."""

    @patch("executors.exec_interactive.pty_runner.start_session")
    def test_start_routes_to_start_session(self, mock_start) -> None:
        mock_start.return_value = {
            "success": True,
            "session_id": "abc123",
            "command": "bash",
            "cols": 80,
            "rows": 24,
            "timeout": 60,
            "initial_output": "$ ",
        }

        stdin = _make_stdin(
            {"type": "start", "command": "bash", "timeout": 60, "cols": 80, "rows": 24},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        mock_start.assert_called_once_with("bash", timeout=60, cols=80, rows=24)

        lines = stdout.getvalue().strip().split("\n")
        msg = json.loads(lines[0])
        assert msg["type"] == "started"
        assert msg["success"] is True
        assert msg["session_id"] == "abc123"

    @patch("executors.exec_interactive.pty_runner.start_session")
    def test_start_uses_defaults(self, mock_start) -> None:
        mock_start.return_value = {"success": True, "session_id": "x"}

        stdin = _make_stdin(
            {"type": "start", "command": "python"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        mock_start.assert_called_once_with("python", timeout=300, cols=120, rows=40)


class TestSendInput:
    """Test send_input message routing."""

    @patch("executors.exec_interactive.pty_runner.send_input")
    def test_send_input_routes(self, mock_send) -> None:
        mock_send.return_value = {
            "success": True,
            "session_id": "abc123",
            "output": "hello\n",
        }

        stdin = _make_stdin(
            {"type": "send_input", "session_id": "abc123", "input": "echo hello\n"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        mock_send.assert_called_once_with("abc123", "echo hello\n")

        lines = stdout.getvalue().strip().split("\n")
        msg = json.loads(lines[0])
        assert msg["type"] == "output"
        assert msg["output"] == "hello\n"


class TestReadOutput:
    """Test read_output message routing."""

    @patch("executors.exec_interactive.pty_runner.read_output")
    def test_read_output_routes(self, mock_read) -> None:
        mock_read.return_value = {
            "success": True,
            "session_id": "abc123",
            "output": "data\n",
            "elapsed": 1.0,
            "remaining": 299.0,
        }

        stdin = _make_stdin(
            {"type": "read_output", "session_id": "abc123", "read_timeout": 5},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        mock_read.assert_called_once_with("abc123", timeout=5.0)


class TestResize:
    """Test resize message routing."""

    @patch("executors.exec_interactive.pty_runner.resize_terminal")
    def test_resize_routes(self, mock_resize) -> None:
        mock_resize.return_value = {
            "success": True,
            "session_id": "abc123",
            "cols": 132,
            "rows": 50,
        }

        stdin = _make_stdin(
            {"type": "resize", "session_id": "abc123", "cols": 132, "rows": 50},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        mock_resize.assert_called_once_with("abc123", 132, 50)

        lines = stdout.getvalue().strip().split("\n")
        msg = json.loads(lines[0])
        assert msg["type"] == "resized"


class TestClose:
    """Test close message routing — exits after close."""

    @patch("executors.exec_interactive.pty_runner.close_session")
    def test_close_routes_and_exits(self, mock_close) -> None:
        mock_close.return_value = {
            "success": True,
            "session_id": "abc123",
            "exit_code": 0,
        }

        stdin = _make_stdin({"type": "close", "session_id": "abc123"})
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        mock_close.assert_called_once_with("abc123")

        lines = stdout.getvalue().strip().split("\n")
        msg = json.loads(lines[0])
        assert msg["type"] == "closed"
        assert msg["exit_code"] == 0


class TestInfo:
    """Test info message routing."""

    @patch("executors.exec_interactive.pty_runner.get_session_info")
    def test_info_routes(self, mock_info) -> None:
        mock_info.return_value = {
            "success": True,
            "session_id": "abc123",
            "command": "bash",
        }

        stdin = _make_stdin(
            {"type": "info", "session_id": "abc123"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        lines = stdout.getvalue().strip().split("\n")
        msg = json.loads(lines[0])
        assert msg["type"] == "session_info"


class TestGetIoLog:
    """Test get_io_log message routing."""

    @patch("executors.exec_interactive.pty_runner.get_io_log")
    def test_get_io_log_routes(self, mock_log) -> None:
        mock_log.return_value = {
            "success": True,
            "session_id": "abc123",
            "io_log": [],
        }

        stdin = _make_stdin(
            {"type": "get_io_log", "session_id": "abc123"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        lines = stdout.getvalue().strip().split("\n")
        msg = json.loads(lines[0])
        assert msg["type"] == "io_log"


class TestUnknownMessage:
    """Test unknown message type."""

    def test_unknown_returns_error(self) -> None:
        stdin = _make_stdin(
            {"type": "bogus"},
            {"type": "shutdown"},
        )
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        lines = stdout.getvalue().strip().split("\n")
        msg = json.loads(lines[0])
        assert msg["type"] == "error"
        assert "Unknown" in msg["message"]


class TestEOF:
    """Test clean exit on EOF."""

    def test_eof_exits_cleanly(self) -> None:
        stdin = StringIO("")
        stdout = StringIO()

        with (
            patch("executors.exec_interactive.pty_runner.sys.stdin", stdin),
            patch("executors.exec_interactive.pty_runner.sys.stdout", stdout),
        ):
            main()

        assert stdout.getvalue() == ""
