"""Tests for the monitor CLI subcommands (creel monitor ...)."""

from __future__ import annotations

import argparse
from pathlib import Path

from creel import cli
from creel.cron.models import Schedule
from creel.monitors.models import (
    Monitor,
    MonitorRunRecord,
    MonitorRunStatus,
)
from creel.monitors.store import MonitorStore

# -- Helpers --


def _make_store(tmp_path: Path) -> MonitorStore:
    return MonitorStore(
        monitors_path=tmp_path / "monitors" / "monitors.json",
        runs_path=tmp_path / "monitors" / "runs.json",
        alerts_path=tmp_path / "monitors" / "alerts.json",
    )


def _make_monitor(name: str = "test monitor", **kwargs) -> Monitor:
    defaults = dict(
        name=name,
        schedule=Schedule(kind="cron", expr="0 8 * * *"),
        executor="gmail_readonly",
        prompt="Check for stuff",
    )
    defaults.update(kwargs)
    return Monitor(**defaults)


def _monitor_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    defaults = dict(
        monitors_dir=tmp_path / "monitors",
        agent_config=tmp_path / "agent.yaml",
        containers=False,
        no_judge=False,
        verbose=False,
        json_logs=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _monitor_store helper
# ---------------------------------------------------------------------------


class TestMonitorStoreHelper:
    def test_creates_store_from_args(self, tmp_path: Path) -> None:
        args = _monitor_args(tmp_path)
        store = cli._monitor_store(args)
        assert isinstance(store, MonitorStore)


# ---------------------------------------------------------------------------
# cmd_monitor_list
# ---------------------------------------------------------------------------


class TestCmdMonitorList:
    def test_empty_list(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(tmp_path)
        rc = cli.cmd_monitor_list(args)
        assert rc == 0
        assert "No monitors" in capsys.readouterr().out

    def test_list_with_monitors(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        store.add(_make_monitor("email watcher"))
        store.add(_make_monitor("disk checker", executor="exec"))

        args = _monitor_args(tmp_path)
        rc = cli.cmd_monitor_list(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "email watcher" in out
        assert "disk checker" in out
        assert "2 monitor(s)" in out


# ---------------------------------------------------------------------------
# cmd_monitor_add
# ---------------------------------------------------------------------------


class TestCmdMonitorAdd:
    def test_add_basic_monitor(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(
            tmp_path,
            name="my monitor",
            executor="exec",
            prompt="check disk",
            cron="0 8 * * *",
            every=None,
            delivery_channel=None,
            delivery_url=None,
            alert_level="notice",
            quiet_hours=None,
            cooldown=3600,
            description="",
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_monitor_add(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Created monitor" in out
        assert "my monitor" in out

    def test_add_with_delivery(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(
            tmp_path,
            name="alert mon",
            executor="gmail_readonly",
            prompt="check email",
            cron="*/15 * * * *",
            every=None,
            delivery_channel="telegram",
            delivery_url=None,
            alert_level="urgent",
            quiet_hours="23:00-07:00",
            cooldown=1800,
            description="Email alerts",
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_monitor_add(args)
        assert rc == 0

        store = _make_store(tmp_path)
        monitors = store.list()
        assert len(monitors) == 1
        assert monitors[0].delivery.mode == "announce"
        assert monitors[0].delivery.channel == "telegram"

    def test_add_no_schedule_fails(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(
            tmp_path,
            name="bad",
            executor="exec",
            prompt="check",
            cron=None,
            every=None,
            delivery_channel=None,
            delivery_url=None,
            alert_level="notice",
            quiet_hours=None,
            cooldown=3600,
            description="",
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_monitor_add(args)
        assert rc == 1
        assert "must specify" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_monitor_add_template
# ---------------------------------------------------------------------------


class TestCmdMonitorAddTemplate:
    def test_add_from_template(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(
            tmp_path,
            template_name="urgent_email",
            delivery_channel="telegram",
            delivery_url=None,
            tz=None,
        )
        rc = cli.cmd_monitor_add_template(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Created monitor" in out
        assert "urgent_email" in out

    def test_add_unknown_template_fails(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(
            tmp_path,
            template_name="nonexistent",
            delivery_channel=None,
            delivery_url=None,
            tz=None,
        )
        rc = cli.cmd_monitor_add_template(args)
        assert rc == 1
        assert "Unknown template" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_monitor_templates
# ---------------------------------------------------------------------------


class TestCmdMonitorTemplates:
    def test_list_templates(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(tmp_path)
        rc = cli.cmd_monitor_templates(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "urgent_email" in out
        assert "calendar_conflicts" in out
        assert "system_health" in out


# ---------------------------------------------------------------------------
# cmd_monitor_enable / disable
# ---------------------------------------------------------------------------


class TestCmdMonitorEnableDisable:
    def test_enable(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        mon = _make_monitor(enabled=False)
        store.add(mon)

        args = _monitor_args(tmp_path, monitor_id=mon.id)
        rc = cli.cmd_monitor_enable(args)
        assert rc == 0
        assert "Enabled" in capsys.readouterr().out

        # Reload from disk to see CLI's changes
        store.load()
        updated = store.get(mon.id)
        assert updated is not None
        assert updated.enabled is True

    def test_disable(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        mon = _make_monitor(enabled=True)
        store.add(mon)

        args = _monitor_args(tmp_path, monitor_id=mon.id)
        rc = cli.cmd_monitor_disable(args)
        assert rc == 0
        assert "Disabled" in capsys.readouterr().out

    def test_enable_nonexistent(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(tmp_path, monitor_id="nonexistent")
        rc = cli.cmd_monitor_enable(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_monitor_remove
# ---------------------------------------------------------------------------


class TestCmdMonitorRemove:
    def test_remove(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        mon = _make_monitor()
        store.add(mon)

        args = _monitor_args(tmp_path, monitor_id=mon.id)
        rc = cli.cmd_monitor_remove(args)
        assert rc == 0
        assert "Removed" in capsys.readouterr().out
        # Reload from disk to see CLI's changes
        store.load()
        assert store.get(mon.id) is None

    def test_remove_nonexistent(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(tmp_path, monitor_id="nonexistent")
        rc = cli.cmd_monitor_remove(args)
        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_monitor_run
# ---------------------------------------------------------------------------


class TestCmdMonitorRun:
    def test_run_monitor(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        mon = _make_monitor()
        store.add(mon)

        args = _monitor_args(tmp_path, monitor_id=mon.id)
        rc = cli.cmd_monitor_run(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Running monitor" in out
        assert "OK" in out

    def test_run_nonexistent(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(tmp_path, monitor_id="nonexistent")
        rc = cli.cmd_monitor_run(args)
        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_monitor_history
# ---------------------------------------------------------------------------


class TestCmdMonitorHistory:
    def test_history_empty(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        mon = _make_monitor()
        store.add(mon)

        args = _monitor_args(tmp_path, monitor_id=mon.id, type="all", tail=20)
        rc = cli.cmd_monitor_history(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No runs" in out

    def test_history_with_runs(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        mon = _make_monitor()
        store.add(mon)

        run = MonitorRunRecord(
            monitor_id=mon.id,
            started_at="2026-03-12T08:00:00+00:00",
            ended_at="2026-03-12T08:00:05+00:00",
            status=MonitorRunStatus.OK,
        )
        store.add_run(run)

        args = _monitor_args(tmp_path, monitor_id=mon.id, type="runs", tail=20)
        rc = cli.cmd_monitor_history(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "ok" in out

    def test_history_nonexistent(self, tmp_path: Path, capsys) -> None:
        args = _monitor_args(tmp_path, monitor_id="nonexistent", type="all", tail=20)
        rc = cli.cmd_monitor_history(args)
        assert rc == 1
