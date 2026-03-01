"""Tests for CLI entry point (cli.py)."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from taskrunner import cli

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestReadPid:
    def test_no_file(self, tmp_path: Path) -> None:
        assert cli._read_pid(tmp_path / "missing.pid") is None

    def test_valid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n")
        assert cli._read_pid(pid_file) == 12345

    def test_invalid_content(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not-a-number\n")
        assert cli._read_pid(pid_file) is None


class TestPidIsRunning:
    def test_alive_process(self) -> None:
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            assert cli._pid_is_running(1234) is True
            mock_kill.assert_called_once_with(1234, 0)

    def test_dead_process(self) -> None:
        with patch("os.kill") as mock_kill:
            mock_kill.side_effect = OSError(errno.ESRCH, "No such process")
            assert cli._pid_is_running(1234) is False

    def test_permission_denied_means_alive(self) -> None:
        with patch("os.kill") as mock_kill:
            mock_kill.side_effect = OSError(errno.EPERM, "Operation not permitted")
            assert cli._pid_is_running(1234) is True


class TestCleanupStaleDaemonFiles:
    def test_removes_existing_files(self, tmp_path: Path) -> None:
        pid = tmp_path / "daemon.pid"
        sock = tmp_path / "daemon.sock"
        pid.write_text("123")
        sock.write_text("x")

        cli._cleanup_stale_daemon_files(pid, sock)
        assert not pid.exists()
        assert not sock.exists()

    def test_no_error_on_missing_files(self, tmp_path: Path) -> None:
        cli._cleanup_stale_daemon_files(tmp_path / "nope.pid", tmp_path / "nope.sock")


class TestAllowLaunchdFailure:
    @pytest.mark.parametrize(
        "output",
        [
            "Could not find service",
            "service not loaded",
            "Not found in domain",
            "No such process",
        ],
    )
    def test_bootout_expected_failures(self, output: str) -> None:
        assert cli._allow_launchd_bootout_failure(output) is True

    def test_bootout_unexpected_failure(self) -> None:
        assert cli._allow_launchd_bootout_failure("something else") is False

    def test_bootstrap_already_loaded(self) -> None:
        assert cli._allow_launchd_bootstrap_failure("Service already loaded") is True

    def test_bootstrap_unexpected(self) -> None:
        assert cli._allow_launchd_bootstrap_failure("other error") is False


class TestBuildDaemonRunCommand:
    def test_basic(self, cli_args) -> None:
        args = cli_args()
        cmd = cli._build_daemon_run_command(args, args.socket_path, args.pid_file)
        assert cmd[0] == cli.sys.executable
        assert "daemon" in cmd
        assert "run" in cmd
        assert "--socket-path" in cmd

    def test_all_flags(self, cli_args) -> None:
        args = cli_args(
            containers=True,
            no_judge=True,
            verbose=True,
            json_logs=True,
            no_scheduler=True,
        )
        cmd = cli._build_daemon_run_command(args, args.socket_path, args.pid_file)
        assert "--containers" in cmd
        assert "--no-judge" in cmd
        assert "--verbose" in cmd
        assert "--json-logs" in cmd
        assert "--no-scheduler" in cmd


class TestBuildDaemonEnv:
    def test_sets_pythonpath(self, tmp_path: Path) -> None:
        env = cli._build_daemon_env(tmp_path)
        assert str(tmp_path / "src") in env["PYTHONPATH"]

    def test_preserves_existing_pythonpath(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PYTHONPATH", "/existing/path")
        env = cli._build_daemon_env(tmp_path)
        assert "/existing/path" in env["PYTHONPATH"]
        assert str(tmp_path / "src") in env["PYTHONPATH"]

    def test_no_duplicate_if_already_present(self, tmp_path: Path, monkeypatch) -> None:
        src_str = str(tmp_path / "src")
        monkeypatch.setenv("PYTHONPATH", src_str)
        env = cli._build_daemon_env(tmp_path)
        # Should not prepend a duplicate
        assert env["PYTHONPATH"] == src_str


class TestBuildDaemonChannel:
    def test_none_channel(self, minimal_agent_def) -> None:
        ch, im = cli._build_daemon_channel(minimal_agent_def, "none")
        assert ch is None
        assert im is None

    def test_imessage_without_config_raises(self, minimal_agent_def) -> None:
        with pytest.raises(ValueError, match="No imessage channel configured"):
            cli._build_daemon_channel(minimal_agent_def, "imessage")

    def test_bluebubbles_without_config_raises(self, minimal_agent_def) -> None:
        with pytest.raises(ValueError, match="No bluebubbles channel configured"):
            cli._build_daemon_channel(minimal_agent_def, "bluebubbles")


class TestLoadAgentDef:
    def test_loads_config(self, tmp_path: Path, cli_args) -> None:
        config = {
            "system_prompt": "test prompt",
            "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
        }
        config_path = tmp_path / "agent.yaml"
        config_path.write_text(yaml.dump(config))
        args = cli_args(agent_config=config_path)
        agent_def = cli._load_agent_def(args)
        assert agent_def.system_prompt == "test prompt"

    def test_no_judge_disables_guardian(self, tmp_path: Path, cli_args) -> None:
        config = {
            "system_prompt": "test",
            "guardian": {
                "enabled": True,
                "llm_judge": {"enabled": True},
            },
        }
        config_path = tmp_path / "agent.yaml"
        config_path.write_text(yaml.dump(config))
        args = cli_args(agent_config=config_path, no_judge=True)
        agent_def = cli._load_agent_def(args)
        assert agent_def.guardian.llm_judge.enabled is False


# ---------------------------------------------------------------------------
# cmd_run tests
# ---------------------------------------------------------------------------


class TestCmdRun:
    def test_task_not_found(self, cli_args, capsys) -> None:
        args = cli_args(task_name="nonexistent")
        rc = cli.cmd_run(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_dry_run(self, cli_args, sample_task_yaml, capsys) -> None:
        task_path = sample_task_yaml()
        args = cli_args(
            task_name="test_task",
            tasks_dir=task_path.parent,
            dry=True,
        )
        with patch("taskrunner.orchestrator._run_executor_inline") as mock_exec:
            mock_exec.return_value = '{"temp": "72"}'
            rc = cli.cmd_run(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Rendered Prompt" in out

    def test_normal_run(self, cli_args, sample_task_yaml, capsys) -> None:
        task_path = sample_task_yaml()
        args = cli_args(
            task_name="test_task",
            tasks_dir=task_path.parent,
            dry=False,
        )
        with (
            patch("taskrunner.orchestrator._run_executor_inline") as mock_exec,
            patch("taskrunner.orchestrator.run_llm") as mock_llm,
            patch("taskrunner.orchestrator.send_output"),
        ):
            mock_exec.return_value = '{"temp": "72"}'
            mock_llm.return_value = "Nice weather!"
            rc = cli.cmd_run(args)
        assert rc == 0
        assert "completed" in capsys.readouterr().out

    def test_verbose_on_success(self, cli_args, sample_task_yaml, capsys) -> None:
        task_path = sample_task_yaml()
        args = cli_args(
            task_name="test_task",
            tasks_dir=task_path.parent,
            dry=False,
            verbose=True,
        )
        with (
            patch("taskrunner.orchestrator._run_executor_inline") as mock_exec,
            patch("taskrunner.orchestrator.run_llm") as mock_llm,
            patch("taskrunner.orchestrator.send_output"),
        ):
            mock_exec.return_value = '{"temp": "72"}'
            mock_llm.return_value = "Nice weather!"
            rc = cli.cmd_run(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Output" in out

    def test_exception_returns_1(self, cli_args, sample_task_yaml, capsys) -> None:
        task_path = sample_task_yaml()
        args = cli_args(
            task_name="test_task",
            tasks_dir=task_path.parent,
            dry=False,
        )
        with patch("taskrunner.cli.run_task", side_effect=RuntimeError("boom")):
            rc = cli.cmd_run(args)
        assert rc == 1
        assert "boom" in capsys.readouterr().err

    def test_exception_verbose_prints_traceback(self, cli_args, sample_task_yaml, capsys) -> None:
        task_path = sample_task_yaml()
        args = cli_args(
            task_name="test_task",
            tasks_dir=task_path.parent,
            dry=False,
            verbose=True,
        )
        with patch("taskrunner.cli.run_task", side_effect=RuntimeError("boom")):
            rc = cli.cmd_run(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "Traceback" in err


# ---------------------------------------------------------------------------
# cmd_schedule tests
# ---------------------------------------------------------------------------


class TestCmdSchedule:
    def test_calls_start_scheduler(self, cli_args) -> None:
        args = cli_args()
        with patch("taskrunner.cli.start_scheduler") as mock_sched:
            cli.cmd_schedule(args)
            mock_sched.assert_called_once()

    def test_keyboard_interrupt_returns_0(self, cli_args, capsys) -> None:
        args = cli_args()
        with patch(
            "taskrunner.cli.start_scheduler",
            side_effect=KeyboardInterrupt,
        ):
            rc = cli.cmd_schedule(args)
        assert rc == 0
        assert "stopped" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# cmd_list tests
# ---------------------------------------------------------------------------


class TestCmdList:
    def test_dir_not_found(self, cli_args, capsys) -> None:
        args = cli_args(tasks_dir=Path("/nonexistent/tasks"))
        rc = cli.cmd_list(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_no_tasks(self, cli_args, tmp_path: Path, capsys) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        args = cli_args(tasks_dir=tasks_dir)
        rc = cli.cmd_list(args)
        assert rc == 0
        assert "No tasks found" in capsys.readouterr().out

    def test_lists_tasks(self, cli_args, sample_task_yaml, capsys) -> None:
        task_path = sample_task_yaml()
        args = cli_args(tasks_dir=task_path.parent)
        rc = cli.cmd_list(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "test_task" in out
        assert "weather" in out

    def test_multiple_tasks(self, cli_args, sample_task_yaml, capsys) -> None:
        sample_task_yaml(name="task_a")
        p = sample_task_yaml(name="task_b")
        args = cli_args(tasks_dir=p.parent)
        rc = cli.cmd_list(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "task_a" in out
        assert "task_b" in out


# ---------------------------------------------------------------------------
# cmd_validate tests
# ---------------------------------------------------------------------------


class TestCmdValidate:
    def test_file_not_found(self, cli_args, capsys) -> None:
        args = cli_args(task_name="nonexistent")
        rc = cli.cmd_validate(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_valid_task(self, cli_args, sample_task_yaml, capsys) -> None:
        p = sample_task_yaml()
        args = cli_args(task_name="test_task", tasks_dir=p.parent)
        rc = cli.cmd_validate(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "valid" in out.lower()
        assert "weather" in out

    def test_invalid_task(self, cli_args, tmp_path: Path, capsys) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        bad_file = tasks_dir / "bad.yaml"
        bad_file.write_text("name: bad\nschedule: nope\n")
        args = cli_args(task_name="bad", tasks_dir=tasks_dir)
        rc = cli.cmd_validate(args)
        assert rc == 1
        assert "failed" in capsys.readouterr().err.lower()

    def test_prints_details(self, cli_args, sample_task_yaml, capsys) -> None:
        p = sample_task_yaml()
        args = cli_args(task_name="test_task", tasks_dir=p.parent)
        cli.cmd_validate(args)
        out = capsys.readouterr().out
        assert "Schedule:" in out
        assert "Executors:" in out
        assert "Output:" in out
        assert "LLM:" in out


# ---------------------------------------------------------------------------
# cmd_daemon_stop tests
# ---------------------------------------------------------------------------


class TestCmdDaemonStop:
    def test_launchd_path(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "darwin")
        args = cli_args()
        args.plist_path.parent.mkdir(parents=True, exist_ok=True)
        args.plist_path.write_text("dummy")

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("subprocess.run", side_effect=fake_run):
            rc = cli.cmd_daemon_stop(args)
        assert rc == 0
        assert "stopped" in capsys.readouterr().out.lower()

    def test_no_pid_not_running(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()
        rc = cli.cmd_daemon_stop(args)
        assert rc == 0
        assert "not running" in capsys.readouterr().out.lower()

    def test_stale_pid_cleanup(self, cli_args, tmp_path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text("99999\n")

        with patch.object(cli, "_pid_is_running", return_value=False):
            rc = cli.cmd_daemon_stop(args)
        assert rc == 0
        assert "stale" in capsys.readouterr().out.lower()

    def test_sigterm_and_wait(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text("42\n")

        call_count = 0

        def pid_running(pid):
            nonlocal call_count
            call_count += 1
            return call_count <= 1  # alive first check, dead second

        with (
            patch.object(cli, "_pid_is_running", side_effect=pid_running),
            patch("os.kill") as mock_kill,
            patch("time.sleep"),
        ):
            rc = cli.cmd_daemon_stop(args)
        assert rc == 0
        mock_kill.assert_called_once_with(42, signal.SIGTERM)

    def test_timeout_returns_1(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args(timeout=0.1)
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text("42\n")

        with (
            patch.object(cli, "_pid_is_running", return_value=True),
            patch("os.kill"),
            patch("time.sleep"),
            patch("time.time", side_effect=[0.0, 0.0, 1.0]),  # immediately past deadline
        ):
            rc = cli.cmd_daemon_stop(args)
        assert rc == 1
        assert "timed out" in capsys.readouterr().err.lower()

    def test_launchd_bootout_failure(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "darwin")
        args = cli_args()
        args.plist_path.parent.mkdir(parents=True, exist_ok=True)
        args.plist_path.write_text("dummy")

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, "", "unexpected error")

        with patch("subprocess.run", side_effect=fake_run):
            rc = cli.cmd_daemon_stop(args)
        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_daemon_status tests
# ---------------------------------------------------------------------------


class TestCmdDaemonStatus:
    def test_not_running(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()
        with patch.object(cli, "_wait_for_daemon_health", return_value=False):
            rc = cli.cmd_daemon_status(args)
        assert rc == 1
        assert "not running" in capsys.readouterr().out.lower()

    def test_running_with_health(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text("42\n")

        status_data = {
            "uptime_seconds": 100,
            "sessions": {"stored": 2, "active_senders": 1},
            "scheduler": {"running": True},
            "channels": [],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = status_data

        with (
            patch.object(cli, "_pid_is_running", return_value=True),
            patch.object(cli, "_daemon_request", return_value=mock_resp),
        ):
            rc = cli.cmd_daemon_status(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "pid 42" in out
        assert "Uptime:" in out
        assert "Sessions:" in out

    def test_api_unhealthy(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text("42\n")

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with (
            patch.object(cli, "_pid_is_running", return_value=True),
            patch.object(cli, "_daemon_request", return_value=mock_resp),
        ):
            rc = cli.cmd_daemon_status(args)
        assert rc == 1
        assert "unhealthy" in capsys.readouterr().err.lower()

    def test_api_unreachable(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text("42\n")

        with (
            patch.object(cli, "_pid_is_running", return_value=True),
            patch.object(cli, "_daemon_request", side_effect=ConnectionError("refused")),
        ):
            rc = cli.cmd_daemon_status(args)
        assert rc == 1
        assert "unreachable" in capsys.readouterr().err.lower()

    def test_channels_display(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text("42\n")

        status_data = {
            "uptime_seconds": 50,
            "sessions": {"stored": 0, "active_senders": 0},
            "scheduler": {"running": False},
            "channels": [
                {"name": "imessage", "running": True, "detail": "polling"},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = status_data

        with (
            patch.object(cli, "_pid_is_running", return_value=True),
            patch.object(cli, "_daemon_request", return_value=mock_resp),
        ):
            rc = cli.cmd_daemon_status(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "imessage" in out
        assert "running" in out.lower()

    def test_pid_unknown_but_healthy(self, cli_args, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        args = cli_args()

        status_data = {
            "uptime_seconds": 50,
            "sessions": {"stored": 0, "active_senders": 0},
            "scheduler": {"running": False},
            "channels": [],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = status_data

        with (
            patch.object(cli, "_wait_for_daemon_health", return_value=True),
            patch.object(cli, "_daemon_request", return_value=mock_resp),
        ):
            rc = cli.cmd_daemon_status(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "pid unknown" in out.lower()


# ---------------------------------------------------------------------------
# cmd_audit tests
# ---------------------------------------------------------------------------


class TestCmdAudit:
    def _make_audit_args(self, cli_args, **overrides):
        defaults = dict(
            tail=20,
            all=False,
            event=None,
            blocked=False,
            denied=False,
            tool=None,
            since=None,
        )
        defaults.update(overrides)
        return cli_args(**defaults)

    def test_no_entries(self, cli_args, tmp_path, capsys) -> None:
        config = {
            "system_prompt": "test",
            "guardian": {
                "enabled": False,
                "audit": {"log_file": str(tmp_path / "audit.jsonl")},
            },
        }
        config_path = tmp_path / "agent.yaml"
        config_path.write_text(yaml.dump(config))
        args = self._make_audit_args(cli_args, agent_config=config_path)
        rc = cli.cmd_audit(args)
        assert rc == 0
        assert "No audit entries" in capsys.readouterr().out

    def test_config_not_found(self, cli_args, capsys) -> None:
        args = self._make_audit_args(
            cli_args,
            agent_config=Path("/nonexistent/agent.yaml"),
        )
        rc = cli.cmd_audit(args)
        assert rc == 1

    def test_screen_input_event(self, cli_args, tmp_path, capsys) -> None:
        import json

        log_file = tmp_path / "audit.jsonl"
        log_file.write_text(
            json.dumps(
                {
                    "ts": "2025-01-01T00:00:00",
                    "event": "screen_input",
                    "blocked": True,
                    "source": "classifier",
                    "confidence": 0.95,
                }
            )
            + "\n"
        )
        config = {
            "system_prompt": "test",
            "guardian": {"enabled": False, "audit": {"log_file": str(log_file)}},
        }
        config_path = tmp_path / "agent.yaml"
        config_path.write_text(yaml.dump(config))
        args = self._make_audit_args(cli_args, agent_config=config_path)
        rc = cli.cmd_audit(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "screen_input" in out
        assert "BLOCKED" in out

    def test_validate_action_event(self, cli_args, tmp_path, capsys) -> None:
        import json

        log_file = tmp_path / "audit.jsonl"
        log_file.write_text(
            json.dumps(
                {
                    "ts": "2025-01-01T00:00:00",
                    "event": "validate_action",
                    "verdict": "allow",
                    "tool_name": "weather",
                    "matched_rule": "allow-weather",
                }
            )
            + "\n"
        )
        config = {
            "system_prompt": "test",
            "guardian": {"enabled": False, "audit": {"log_file": str(log_file)}},
        }
        config_path = tmp_path / "agent.yaml"
        config_path.write_text(yaml.dump(config))
        args = self._make_audit_args(cli_args, agent_config=config_path)
        rc = cli.cmd_audit(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "validate_action" in out
        assert "allow" in out
        assert "weather" in out

    def test_tool_result_event(self, cli_args, tmp_path, capsys) -> None:
        import json

        log_file = tmp_path / "audit.jsonl"
        log_file.write_text(
            json.dumps(
                {
                    "ts": "2025-01-01T00:00:00",
                    "event": "tool_result",
                    "success": True,
                    "tool_name": "weather",
                    "duration_ms": 123,
                    "output_length": 456,
                }
            )
            + "\n"
        )
        config = {
            "system_prompt": "test",
            "guardian": {"enabled": False, "audit": {"log_file": str(log_file)}},
        }
        config_path = tmp_path / "agent.yaml"
        config_path.write_text(yaml.dump(config))
        args = self._make_audit_args(cli_args, agent_config=config_path)
        rc = cli.cmd_audit(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "tool_result" in out
        assert "weather" in out


# ---------------------------------------------------------------------------
# cmd_send (non-streaming) tests
# ---------------------------------------------------------------------------


class TestCmdSendNonStreaming:
    def _make_send_args(self, cli_args, **overrides):
        defaults = dict(
            sender_id="cli",
            message="hello",
            session_id=None,
            timeout=5.0,
            stream=False,
            auto_approve=False,
        )
        defaults.update(overrides)
        return cli_args(**defaults)

    def test_success(self, cli_args, capsys) -> None:
        args = self._make_send_args(cli_args)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "hi there"}

        with patch.object(cli, "_daemon_request", return_value=mock_resp):
            rc = cli.cmd_send(args)
        assert rc == 0
        assert "hi there" in capsys.readouterr().out

    def test_connection_error(self, cli_args, capsys) -> None:
        args = self._make_send_args(cli_args)
        with patch.object(cli, "_daemon_request", side_effect=ConnectionError("refused")):
            rc = cli.cmd_send(args)
        assert rc == 1
        assert "refused" in capsys.readouterr().err

    def test_api_error(self, cli_args, capsys) -> None:
        args = self._make_send_args(cli_args)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"detail": "server error"}

        with patch.object(cli, "_daemon_request", return_value=mock_resp):
            rc = cli.cmd_send(args)
        assert rc == 1
        assert "500" in capsys.readouterr().err

    def test_with_session_id(self, cli_args) -> None:
        args = self._make_send_args(cli_args, session_id="sess-123")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "ok"}

        with patch.object(cli, "_daemon_request", return_value=mock_resp) as mock_req:
            cli.cmd_send(args)
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["json_body"]["session_id"] == "sess-123"


# ---------------------------------------------------------------------------
# cmd_encrypt tests
# ---------------------------------------------------------------------------


class TestCmdEncrypt:
    def test_encrypt_basic(self, cli_args, tmp_path, age_keypair, capsys) -> None:
        _, pub_file = age_keypair
        env_file = tmp_path / "test.env"
        env_file.write_text("SECRET=hunter2\n")

        args = cli_args(env_file=str(env_file), recipient=str(pub_file), output=None, delete=False)
        rc = cli.cmd_encrypt(args)
        assert rc == 0
        assert (tmp_path / "test.env.enc").exists()
        assert env_file.exists()  # not deleted
        out = capsys.readouterr().out
        assert "Encrypted:" in out
        assert "Delete the plaintext" in out

    def test_encrypt_delete(self, cli_args, tmp_path, age_keypair, capsys) -> None:
        _, pub_file = age_keypair
        env_file = tmp_path / "test.env"
        env_file.write_text("SECRET=hunter2\n")

        args = cli_args(env_file=str(env_file), recipient=str(pub_file), output=None, delete=True)
        rc = cli.cmd_encrypt(args)
        assert rc == 0
        assert (tmp_path / "test.env.enc").exists()
        assert not env_file.exists()  # deleted
        out = capsys.readouterr().out
        assert "Deleted plaintext" in out

    def test_encrypt_custom_output(self, cli_args, tmp_path, age_keypair) -> None:
        _, pub_file = age_keypair
        env_file = tmp_path / "test.env"
        env_file.write_text("KEY=val\n")
        custom_out = tmp_path / "custom.enc"

        args = cli_args(
            env_file=str(env_file),
            recipient=str(pub_file),
            output=str(custom_out),
            delete=False,
        )
        rc = cli.cmd_encrypt(args)
        assert rc == 0
        assert custom_out.exists()

    def test_encrypt_missing_file(self, cli_args, tmp_path, capsys) -> None:
        args = cli_args(
            env_file=str(tmp_path / "missing.env"),
            recipient=None,
            output=None,
            delete=False,
        )
        rc = cli.cmd_encrypt(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err.lower()

    def test_encrypt_via_main(self, monkeypatch, tmp_path, age_keypair, capsys) -> None:
        _, pub_file = age_keypair
        env_file = tmp_path / "test.env"
        env_file.write_text("TOK=abc\n")

        monkeypatch.setattr(
            "sys.argv",
            ["creel", "encrypt", str(env_file), "--recipient", str(pub_file)],
        )
        rc = cli.main()
        assert rc == 0
        assert (tmp_path / "test.env.enc").exists()


# ---------------------------------------------------------------------------
# main() dispatcher tests
# ---------------------------------------------------------------------------


class TestMain:
    def test_no_command_shows_help(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("sys.argv", ["creel"])
        rc = cli.main()
        assert rc == 1

    def test_dispatches_to_list(self, monkeypatch, tmp_path, capsys) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        monkeypatch.setattr(
            "sys.argv",
            ["creel", "--tasks-dir", str(tasks_dir), "list"],
        )
        rc = cli.main()
        assert rc == 0
        assert "No tasks found" in capsys.readouterr().out

    def test_dispatches_to_validate(self, monkeypatch, sample_task_yaml, capsys) -> None:
        p = sample_task_yaml()
        monkeypatch.setattr(
            "sys.argv",
            ["creel", "--tasks-dir", str(p.parent), "validate", "test_task"],
        )
        rc = cli.main()
        assert rc == 0
        assert "valid" in capsys.readouterr().out.lower()

    def test_loads_dot_env(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        env_file = tmp_path / ".env"
        env_file.write_text("MY_TEST_VAR=hello_from_env\n")
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        monkeypatch.setattr(
            "sys.argv",
            ["creel", "--tasks-dir", str(tasks_dir), "list"],
        )
        monkeypatch.delenv("MY_TEST_VAR", raising=False)
        cli.main()
        assert os.environ.get("MY_TEST_VAR") == "hello_from_env"

    def test_daemon_subcommand_dispatches(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        monkeypatch.setattr(
            "sys.argv",
            [
                "creel",
                "daemon",
                "status",
                "--socket-path",
                str(tmp_path / "daemon.sock"),
                "--pid-file",
                str(tmp_path / "daemon.pid"),
            ],
        )
        with patch.object(cli, "_wait_for_daemon_health", return_value=False):
            rc = cli.main()
        assert rc == 1
        assert "not running" in capsys.readouterr().out.lower()
