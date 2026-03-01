"""Tests for the cron CLI subcommands (creel cron ...)."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from creel import cli
from creel.cron.models import (
    CronJob,
    Payload,
    RunRecord,
    RunStatus,
    Schedule,
)
from creel.cron.store import JobStore

# -- Helpers --


def _make_store(tmp_path: Path) -> JobStore:
    return JobStore(
        jobs_path=tmp_path / "cron" / "jobs.json",
        runs_path=tmp_path / "cron" / "runs.json",
    )


def _make_job(name: str = "test job", **kwargs) -> CronJob:
    defaults = dict(
        name=name,
        schedule=Schedule(kind="cron", expr="0 8 * * *"),
        payload=Payload(message="do stuff"),
    )
    defaults.update(kwargs)
    return CronJob(**defaults)


def _cron_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    """Build an argparse.Namespace with common cron CLI attributes."""
    defaults = dict(
        cron_dir=tmp_path / "cron",
        tasks_dir=tmp_path / "tasks",
        agent_config=tmp_path / "agent.yaml",
        containers=False,
        no_judge=False,
        verbose=False,
        json_logs=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _cron_store helper
# ---------------------------------------------------------------------------


class TestCronStoreHelper:
    def test_creates_store_from_args(self, tmp_path: Path) -> None:
        args = _cron_args(tmp_path)
        store = cli._cron_store(args)
        assert isinstance(store, JobStore)

    def test_uses_default_when_no_cron_dir(self, tmp_path: Path) -> None:
        args = argparse.Namespace()
        # Should fall back to default without error
        store = cli._cron_store(args)
        assert isinstance(store, JobStore)


# ---------------------------------------------------------------------------
# _format_schedule helper
# ---------------------------------------------------------------------------


class TestFormatSchedule:
    def test_cron(self) -> None:
        s = Schedule(kind="cron", expr="0 8 * * *")
        assert cli._format_schedule(s) == "cron: 0 8 * * *"

    def test_every(self) -> None:
        s = Schedule(kind="every", expr="300")
        assert cli._format_schedule(s) == "every 300s"

    def test_at(self) -> None:
        s = Schedule(kind="at", expr="2026-03-01T09:00:00")
        assert cli._format_schedule(s) == "at 2026-03-01T09:00:00"


# ---------------------------------------------------------------------------
# cmd_cron_list
# ---------------------------------------------------------------------------


class TestCmdCronList:
    def test_empty_list(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(tmp_path)
        rc = cli.cmd_cron_list(args)
        assert rc == 0
        assert "No cron jobs" in capsys.readouterr().out

    def test_lists_jobs(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Morning briefing")
        store.add(job)

        args = _cron_args(tmp_path)
        rc = cli.cmd_cron_list(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Morning briefing" in out
        assert job.id in out
        assert "1 job(s)" in out

    def test_lists_multiple_jobs(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        store.add(_make_job("Job A"))
        store.add(_make_job("Job B"))

        args = _cron_args(tmp_path)
        rc = cli.cmd_cron_list(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Job A" in out
        assert "Job B" in out
        assert "2 job(s)" in out

    def test_shows_disabled_status(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        store.add(_make_job("Disabled job", enabled=False))

        args = _cron_args(tmp_path)
        rc = cli.cmd_cron_list(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "no" in out  # enabled = no


# ---------------------------------------------------------------------------
# cmd_cron_add
# ---------------------------------------------------------------------------


class TestCmdCronAdd:
    def test_add_cron_job(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="Daily check",
            cron="0 8 * * *",
            every=None,
            at=None,
            message="Run daily check",
            system_event=None,
            model=None,
            timeout_seconds=None,
            target="isolated",
            delivery_mode="none",
            delivery_channel=None,
            delivery_url=None,
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Created job 'Daily check'" in out

        # Verify persisted
        store = _make_store(tmp_path)
        jobs = store.list()
        assert len(jobs) == 1
        assert jobs[0].name == "Daily check"
        assert jobs[0].schedule.kind == "cron"
        assert jobs[0].schedule.expr == "0 8 * * *"

    def test_add_every_job(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="Periodic",
            cron=None,
            every=300,
            at=None,
            message="Check status",
            system_event=None,
            model=None,
            timeout_seconds=None,
            target="isolated",
            delivery_mode="none",
            delivery_channel=None,
            delivery_url=None,
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 0

        store = _make_store(tmp_path)
        jobs = store.list()
        assert len(jobs) == 1
        assert jobs[0].schedule.kind == "every"
        assert jobs[0].schedule.expr == "300"

    def test_add_at_job(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="One-shot",
            cron=None,
            every=None,
            at="2026-03-01T09:00:00",
            message="Remind me",
            system_event=None,
            model=None,
            timeout_seconds=None,
            target="isolated",
            delivery_mode="none",
            delivery_channel=None,
            delivery_url=None,
            tz="America/Denver",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 0

        store = _make_store(tmp_path)
        jobs = store.list()
        assert jobs[0].schedule.kind == "at"
        assert jobs[0].schedule.tz == "America/Denver"

    def test_add_system_event_forces_main_target(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="Reminder",
            cron="0 9 * * *",
            every=None,
            at=None,
            message=None,
            system_event="Time to check email",
            model=None,
            timeout_seconds=None,
            target="isolated",  # should be overridden to main
            delivery_mode="none",
            delivery_channel=None,
            delivery_url=None,
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 0

        store = _make_store(tmp_path)
        jobs = store.list()
        assert jobs[0].target == "main"
        assert jobs[0].payload.kind == "systemEvent"

    def test_add_with_model_override(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="Custom model",
            cron="0 8 * * *",
            every=None,
            at=None,
            message="Do something",
            system_event=None,
            model="claude-opus-4-20250514",
            timeout_seconds=60,
            target="isolated",
            delivery_mode="none",
            delivery_channel=None,
            delivery_url=None,
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 0

        store = _make_store(tmp_path)
        jobs = store.list()
        assert jobs[0].payload.model == "claude-opus-4-20250514"
        assert jobs[0].payload.timeout_seconds == 60

    def test_add_disabled(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="Disabled job",
            cron="0 8 * * *",
            every=None,
            at=None,
            message="do stuff",
            system_event=None,
            model=None,
            timeout_seconds=None,
            target="isolated",
            delivery_mode="none",
            delivery_channel=None,
            delivery_url=None,
            tz="UTC",
            disabled=True,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 0

        store = _make_store(tmp_path)
        jobs = store.list()
        assert jobs[0].enabled is False

    def test_add_no_schedule_fails(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="No schedule",
            cron=None,
            every=None,
            at=None,
            message="do stuff",
            system_event=None,
            model=None,
            timeout_seconds=None,
            target="isolated",
            delivery_mode="none",
            delivery_channel=None,
            delivery_url=None,
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 1
        assert "must specify" in capsys.readouterr().err

    def test_add_no_message_fails(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="No message",
            cron="0 8 * * *",
            every=None,
            at=None,
            message=None,
            system_event=None,
            model=None,
            timeout_seconds=None,
            target="isolated",
            delivery_mode="none",
            delivery_channel=None,
            delivery_url=None,
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 1
        assert "must specify" in capsys.readouterr().err

    def test_add_with_announce_delivery(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="Announced",
            cron="0 8 * * *",
            every=None,
            at=None,
            message="hello",
            system_event=None,
            model=None,
            timeout_seconds=None,
            target="isolated",
            delivery_mode="announce",
            delivery_channel="whatsapp",
            delivery_url=None,
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 0

        store = _make_store(tmp_path)
        jobs = store.list()
        assert jobs[0].delivery.mode == "announce"
        assert jobs[0].delivery.channel == "whatsapp"

    def test_add_with_webhook_delivery(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            name="Webhook",
            cron="0 8 * * *",
            every=None,
            at=None,
            message="hello",
            system_event=None,
            model=None,
            timeout_seconds=None,
            target="isolated",
            delivery_mode="webhook",
            delivery_channel=None,
            delivery_url="https://example.com/hook",
            tz="UTC",
            disabled=False,
        )
        rc = cli.cmd_cron_add(args)
        assert rc == 0

        store = _make_store(tmp_path)
        jobs = store.list()
        assert jobs[0].delivery.mode == "webhook"
        assert jobs[0].delivery.url == "https://example.com/hook"


# ---------------------------------------------------------------------------
# cmd_cron_edit
# ---------------------------------------------------------------------------


class TestCmdCronEdit:
    def test_edit_name(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Original")
        store.add(job)

        args = _cron_args(
            tmp_path,
            job_id=job.id,
            name="Renamed",
            cron=None,
            every=None,
            at=None,
            tz=None,
            enable=False,
            disable=False,
        )
        rc = cli.cmd_cron_edit(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Updated job 'Renamed'" in out

    def test_edit_disable(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Active", enabled=True)
        store.add(job)

        args = _cron_args(
            tmp_path,
            job_id=job.id,
            name=None,
            cron=None,
            every=None,
            at=None,
            tz=None,
            enable=False,
            disable=True,
        )
        rc = cli.cmd_cron_edit(args)
        assert rc == 0

        store2 = _make_store(tmp_path)
        updated = store2.get(job.id)
        assert updated.enabled is False

    def test_edit_enable(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Disabled", enabled=False)
        store.add(job)

        args = _cron_args(
            tmp_path,
            job_id=job.id,
            name=None,
            cron=None,
            every=None,
            at=None,
            tz=None,
            enable=True,
            disable=False,
        )
        rc = cli.cmd_cron_edit(args)
        assert rc == 0

        store2 = _make_store(tmp_path)
        updated = store2.get(job.id)
        assert updated.enabled is True

    def test_edit_schedule_cron(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Reschedule")
        store.add(job)

        args = _cron_args(
            tmp_path,
            job_id=job.id,
            name=None,
            cron="30 9 * * *",
            every=None,
            at=None,
            tz=None,
            enable=False,
            disable=False,
        )
        rc = cli.cmd_cron_edit(args)
        assert rc == 0

        store2 = _make_store(tmp_path)
        updated = store2.get(job.id)
        assert updated.schedule.expr == "30 9 * * *"

    def test_edit_schedule_every(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Interval")
        store.add(job)

        args = _cron_args(
            tmp_path,
            job_id=job.id,
            name=None,
            cron=None,
            every=600,
            at=None,
            tz=None,
            enable=False,
            disable=False,
        )
        rc = cli.cmd_cron_edit(args)
        assert rc == 0

        store2 = _make_store(tmp_path)
        updated = store2.get(job.id)
        assert updated.schedule.kind == "every"
        assert updated.schedule.expr == "600"

    def test_edit_not_found(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(
            tmp_path,
            job_id="nonexistent",
            name=None,
            cron=None,
            every=None,
            at=None,
            tz=None,
            enable=False,
            disable=False,
        )
        rc = cli.cmd_cron_edit(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_edit_no_changes(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Unchanged")
        store.add(job)

        args = _cron_args(
            tmp_path,
            job_id=job.id,
            name=None,
            cron=None,
            every=None,
            at=None,
            tz=None,
            enable=False,
            disable=False,
        )
        rc = cli.cmd_cron_edit(args)
        assert rc == 0
        assert "No changes" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_cron_remove
# ---------------------------------------------------------------------------


class TestCmdCronRemove:
    def test_remove_existing(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("To delete")
        store.add(job)

        args = _cron_args(tmp_path, job_id=job.id)
        rc = cli.cmd_cron_remove(args)
        assert rc == 0
        assert "Removed job" in capsys.readouterr().out

        store2 = _make_store(tmp_path)
        assert store2.get(job.id) is None

    def test_remove_not_found(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(tmp_path, job_id="nonexistent")
        rc = cli.cmd_cron_remove(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_cron_run
# ---------------------------------------------------------------------------


class TestCmdCronRun:
    def test_run_main_session_job(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job(
            "Main reminder",
            target="main",
            payload=Payload(kind="systemEvent", message="Check email"),
        )
        store.add(job)

        args = _cron_args(tmp_path, job_id=job.id)
        rc = cli.cmd_cron_run(args)
        assert rc == 0
        out = capsys.readouterr().out
        # The inject_event prints to stdout
        assert "Check email" in out
        assert "completed successfully" in out

    def test_run_not_found(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(tmp_path, job_id="nonexistent")
        rc = cli.cmd_cron_run(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_run_records_history(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job(
            "Tracked",
            target="main",
            payload=Payload(kind="systemEvent", message="event"),
        )
        store.add(job)

        args = _cron_args(tmp_path, job_id=job.id)
        cli.cmd_cron_run(args)

        store2 = _make_store(tmp_path)
        runs = store2.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS


# ---------------------------------------------------------------------------
# cmd_cron_runs
# ---------------------------------------------------------------------------


class TestCmdCronRuns:
    def test_no_runs(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("No runs")
        store.add(job)

        args = _cron_args(tmp_path, job_id=job.id, tail=20)
        rc = cli.cmd_cron_runs(args)
        assert rc == 0
        assert "No runs recorded" in capsys.readouterr().out

    def test_shows_runs(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Has runs")
        store.add(job)

        store.add_run(
            RunRecord(
                job_id=job.id,
                started_at="2026-01-15T08:00:00+00:00",
                ended_at="2026-01-15T08:00:05+00:00",
                status=RunStatus.SUCCESS,
            )
        )
        store.add_run(
            RunRecord(
                job_id=job.id,
                started_at="2026-01-16T08:00:00+00:00",
                ended_at="2026-01-16T08:00:03+00:00",
                status=RunStatus.FAILURE,
                error="timeout",
            )
        )

        args = _cron_args(tmp_path, job_id=job.id, tail=20)
        rc = cli.cmd_cron_runs(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "success" in out
        assert "failure" in out
        assert "timeout" in out
        assert "2 run(s) shown" in out

    def test_tail_limits_output(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Many runs")
        store.add(job)

        for i in range(10):
            store.add_run(
                RunRecord(
                    job_id=job.id,
                    started_at=f"2026-01-{i + 1:02d}T08:00:00+00:00",
                    ended_at=f"2026-01-{i + 1:02d}T08:00:01+00:00",
                    status=RunStatus.SUCCESS,
                )
            )

        args = _cron_args(tmp_path, job_id=job.id, tail=3)
        rc = cli.cmd_cron_runs(args)
        assert rc == 0
        assert "3 run(s) shown" in capsys.readouterr().out

    def test_not_found(self, tmp_path: Path, capsys) -> None:
        args = _cron_args(tmp_path, job_id="nonexistent", tail=20)
        rc = cli.cmd_cron_runs(args)
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_shows_duration(self, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Duration test")
        store.add(job)

        store.add_run(
            RunRecord(
                job_id=job.id,
                started_at="2026-01-15T08:00:00+00:00",
                ended_at="2026-01-15T08:00:12+00:00",
                status=RunStatus.SUCCESS,
            )
        )

        args = _cron_args(tmp_path, job_id=job.id, tail=20)
        rc = cli.cmd_cron_runs(args)
        assert rc == 0
        assert "12.0s" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() integration — verify argparse wiring
# ---------------------------------------------------------------------------


class TestCronMainDispatch:
    def test_cron_list_via_main(self, monkeypatch, tmp_path: Path, capsys) -> None:
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        monkeypatch.setattr(
            "sys.argv",
            ["creel", "--tasks-dir", str(tmp_path / "tasks"), "cron", "list"],
        )
        # Patch _cron_store to use tmp_path
        with patch.object(
            cli,
            "_cron_store",
            return_value=_make_store(tmp_path),
        ):
            rc = cli.main()
        assert rc == 0
        assert "No cron jobs" in capsys.readouterr().out

    def test_cron_add_via_main(self, monkeypatch, tmp_path: Path, capsys) -> None:
        monkeypatch.setattr(
            "sys.argv",
            [
                "creel",
                "cron",
                "add",
                "--name",
                "Test job",
                "--cron",
                "0 8 * * *",
                "--message",
                "Do stuff",
            ],
        )
        with patch.object(
            cli,
            "_cron_store",
            return_value=_make_store(tmp_path),
        ):
            rc = cli.main()
        assert rc == 0
        assert "Created job" in capsys.readouterr().out

    def test_cron_remove_via_main(self, monkeypatch, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Deletable")
        store.add(job)

        monkeypatch.setattr(
            "sys.argv",
            ["creel", "cron", "remove", job.id],
        )
        with patch.object(cli, "_cron_store", return_value=store):
            rc = cli.main()
        assert rc == 0
        assert "Removed job" in capsys.readouterr().out

    def test_cron_edit_via_main(self, monkeypatch, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Editable")
        store.add(job)

        monkeypatch.setattr(
            "sys.argv",
            ["creel", "cron", "edit", job.id, "--disable"],
        )
        with patch.object(cli, "_cron_store", return_value=store):
            rc = cli.main()
        assert rc == 0
        assert "Updated job" in capsys.readouterr().out

    def test_cron_runs_via_main(self, monkeypatch, tmp_path: Path, capsys) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Runnable")
        store.add(job)

        monkeypatch.setattr(
            "sys.argv",
            ["creel", "cron", "runs", job.id],
        )
        with patch.object(cli, "_cron_store", return_value=store):
            rc = cli.main()
        assert rc == 0
        assert "No runs recorded" in capsys.readouterr().out

    def test_cron_no_subcommand_shows_help(self, monkeypatch, tmp_path: Path, capsys) -> None:
        monkeypatch.setattr("sys.argv", ["creel", "cron"])
        rc = cli.main()
        assert rc == 1
