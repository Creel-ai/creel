"""Tests for launchd daemon lifecycle commands."""

from __future__ import annotations

import argparse
import plistlib
import subprocess
from pathlib import Path
from unittest.mock import patch

from creel import cli


def _make_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        tasks_dir=tmp_path / "tasks",
        agent_config=tmp_path / "agent.yaml",
        containers=False,
        no_judge=False,
        verbose=False,
        json_logs=False,
        channel_type="imessage",
        no_scheduler=False,
        socket_path=tmp_path / "daemon.sock",
        pid_file=tmp_path / "daemon.pid",
        log_file=tmp_path / "daemon.log",
        wait_seconds=1.0,
        timeout=1.0,
        label="com.creel.daemon.test",
        plist_path=tmp_path / "LaunchAgents" / "com.creel.daemon.test.plist",
    )


def _ok_completed(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_daemon_install_non_macos(tmp_path: Path, monkeypatch) -> None:
    args = _make_args(tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "linux")

    rc = cli.cmd_daemon_install(args)
    assert rc == 1
    assert not args.plist_path.exists()


def test_daemon_install_writes_plist_and_calls_launchctl(tmp_path: Path, monkeypatch) -> None:
    args = _make_args(tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_wait_for_daemon_health", lambda _p, _w: True)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], capture_output: bool = True, text: bool = True):
        del capture_output, text
        calls.append(cmd)
        if cmd[1] == "bootout":
            return subprocess.CompletedProcess(cmd, 1, "", "Could not find service")
        return _ok_completed(cmd)

    with patch("subprocess.run", side_effect=fake_run):
        rc = cli.cmd_daemon_install(args)

    assert rc == 0
    assert args.plist_path.exists()

    with args.plist_path.open("rb") as f:
        payload = plistlib.load(f)

    assert payload["Label"] == args.label
    assert "daemon" in payload["ProgramArguments"]
    assert "run" in payload["ProgramArguments"]
    assert payload["StandardOutPath"] == str(args.log_file)
    assert payload["StandardErrorPath"] == str(args.log_file)
    # Properly installed package doesn't need WorkingDirectory or PYTHONPATH
    assert "WorkingDirectory" not in payload
    assert "EnvironmentVariables" not in payload

    launchctl_cmds = [c for c in calls if c and c[0] == "launchctl"]
    assert any(c[1] == "bootstrap" for c in launchctl_cmds)
    assert any(c[1] == "kickstart" for c in launchctl_cmds)


def test_daemon_uninstall_removes_plist_and_unloads_service(tmp_path: Path, monkeypatch) -> None:
    args = _make_args(tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    args.plist_path.parent.mkdir(parents=True, exist_ok=True)
    args.plist_path.write_text("dummy")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], capture_output: bool = True, text: bool = True):
        del capture_output, text
        calls.append(cmd)
        return _ok_completed(cmd)

    with patch("subprocess.run", side_effect=fake_run):
        rc = cli.cmd_daemon_uninstall(args)

    assert rc == 0
    assert not args.plist_path.exists()
    assert any(c[:2] == ["launchctl", "bootout"] for c in calls)


def test_daemon_start_uses_launchd_when_plist_exists(tmp_path: Path, monkeypatch) -> None:
    args = _make_args(tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_wait_for_daemon_health", lambda _p, _w: True)
    monkeypatch.setattr(cli, "_read_pid", lambda _p: 4242)
    args.plist_path.parent.mkdir(parents=True, exist_ok=True)
    args.plist_path.write_text("plist")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], capture_output: bool = True, text: bool = True):
        del capture_output, text
        calls.append(cmd)
        if cmd[1] == "bootstrap":
            return subprocess.CompletedProcess(cmd, 1, "", "Service already loaded")
        return _ok_completed(cmd)

    with patch("subprocess.run", side_effect=fake_run):
        with patch("subprocess.Popen") as popen:
            rc = cli.cmd_daemon_start(args)
            popen.assert_not_called()

    assert rc == 0
    assert any(c[1] == "kickstart" for c in calls)
