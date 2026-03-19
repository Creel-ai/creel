"""Tests for CronManager — scheduler integration and job lifecycle."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from creel.cron.manager import CronManager, _make_trigger, _validate_cron_expr
from creel.cron.models import (
    CronJob,
    Payload,
    RunStatus,
    Schedule,
)
from creel.cron.store import JobStore

# -- Helpers --


def _make_store(tmp_path) -> JobStore:
    return JobStore(
        jobs_path=tmp_path / "jobs.json",
        runs_path=tmp_path / "runs.json",
    )


def _make_job(name: str = "test job", **kwargs) -> CronJob:
    defaults = dict(
        name=name,
        schedule=Schedule(kind="cron", expr="0 8 * * *"),
        payload=Payload(message="do stuff"),
    )
    defaults.update(kwargs)
    return CronJob(**defaults)


# -- _make_trigger tests --


class TestMakeTrigger:
    def test_cron_trigger(self):
        schedule = Schedule(kind="cron", expr="0 8 * * *")
        trigger = _make_trigger(schedule)
        from apscheduler.triggers.cron import CronTrigger

        assert isinstance(trigger, CronTrigger)

    def test_cron_trigger_with_timezone(self):
        schedule = Schedule(kind="cron", expr="0 8 * * *", tz="America/Denver")
        trigger = _make_trigger(schedule)
        from apscheduler.triggers.cron import CronTrigger

        assert isinstance(trigger, CronTrigger)

    def test_interval_trigger(self):
        schedule = Schedule(kind="every", expr="300")
        trigger = _make_trigger(schedule)
        from apscheduler.triggers.interval import IntervalTrigger

        assert isinstance(trigger, IntervalTrigger)

    def test_date_trigger(self):
        schedule = Schedule(kind="at", expr="2026-03-01T09:00:00-07:00")
        trigger = _make_trigger(schedule)
        from apscheduler.triggers.date import DateTrigger

        assert isinstance(trigger, DateTrigger)


# -- _validate_cron_expr tests --


class TestValidateCronExpr:
    def test_valid_standard_5_field(self):
        """Standard 5-field cron expressions should pass."""
        _validate_cron_expr("0 8 * * *")
        _validate_cron_expr("*/5 * * * *")
        _validate_cron_expr("0 0 1,15 * MON-FRI")

    def test_valid_6_and_7_field(self):
        """Extended 6- and 7-field cron expressions should pass."""
        _validate_cron_expr("0 0 8 * * MON")  # 6 fields
        _validate_cron_expr("0 0 8 * * MON 2026")  # 7 fields

    def test_rejects_too_long(self):
        expr = "* " * 51  # 102 chars, over the 100 limit
        with pytest.raises(ValueError, match="too long"):
            _validate_cron_expr(expr.strip())

    def test_rejects_disallowed_characters(self):
        with pytest.raises(ValueError, match="disallowed characters"):
            _validate_cron_expr("0 8 * * *; rm -rf /")

    def test_rejects_shell_metacharacters(self):
        for bad in ["0 8 * * $(cmd)", "0 8 * * `cmd`", "0 8 & * *", "0|8 * * * *"]:
            with pytest.raises(ValueError, match="disallowed characters"):
                _validate_cron_expr(bad)

    def test_rejects_too_few_fields(self):
        with pytest.raises(ValueError, match="must have 5-7 fields"):
            _validate_cron_expr("0 8 * *")

    def test_rejects_too_many_fields(self):
        with pytest.raises(ValueError, match="must have 5-7 fields"):
            _validate_cron_expr("0 0 8 * * MON 2026 extra")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="disallowed characters"):
            _validate_cron_expr("")

    def test_make_trigger_calls_validation(self):
        """_make_trigger should reject bad cron expressions before APScheduler."""
        schedule = Schedule.__new__(Schedule)
        object.__setattr__(schedule, "kind", "cron")
        object.__setattr__(schedule, "expr", "0 8 * * *; echo pwned")
        object.__setattr__(schedule, "tz", "UTC")
        with pytest.raises(ValueError, match="disallowed characters"):
            _make_trigger(schedule)


# -- CronManager lifecycle tests --


class TestCronManagerLifecycle:
    def test_start_and_shutdown(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()
        assert mgr.running is True
        mgr.shutdown()
        assert mgr.running is False

    def test_shutdown_when_not_running(self, tmp_path):
        """Shutdown on a not-started manager should not raise."""
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.shutdown()  # Should be a no-op

    def test_start_loads_enabled_jobs(self, tmp_path):
        store = _make_store(tmp_path)
        job = _make_job("active")
        store.add(job)
        disabled = _make_job("disabled", enabled=False)
        store.add(disabled)

        mgr = CronManager(store)
        mgr.start()

        # Active job should be scheduled
        scheduled = mgr._scheduler.get_jobs()
        scheduled_ids = {j.id for j in scheduled}
        assert job.id in scheduled_ids
        assert disabled.id not in scheduled_ids

        mgr.shutdown()


# -- CRUD tests --


class TestCronManagerCRUD:
    def test_add_job(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job("new job")
        result = mgr.add_job(job)
        assert result.id == job.id
        assert mgr.get_job(job.id) is not None

        mgr.shutdown()

    def test_add_job_schedules_when_running(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job("scheduled")
        mgr.add_job(job)

        scheduled_ids = {j.id for j in mgr._scheduler.get_jobs()}
        assert job.id in scheduled_ids

        mgr.shutdown()

    def test_add_disabled_job_not_scheduled(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job("disabled", enabled=False)
        mgr.add_job(job)

        scheduled_ids = {j.id for j in mgr._scheduler.get_jobs()}
        assert job.id not in scheduled_ids

        mgr.shutdown()

    def test_get_job(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        job = _make_job()
        store.add(job)

        assert mgr.get_job(job.id).name == job.name
        assert mgr.get_job("nonexistent") is None

    def test_list_jobs_empty(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        assert mgr.list_jobs() == []

    def test_list_jobs_returns_all(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        j1 = _make_job("alpha")
        j2 = _make_job("beta")
        store.add(j1)
        store.add(j2)

        jobs = mgr.list_jobs()
        names = {j.name for j in jobs}
        assert names == {"alpha", "beta"}

    def test_update_job(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job("original")
        mgr.add_job(job)
        updated = mgr.update_job(job.id, name="renamed")
        assert updated.name == "renamed"
        assert mgr.get_job(job.id).name == "renamed"

        mgr.shutdown()

    def test_update_job_reschedules(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job("original")
        mgr.add_job(job)
        # Update the schedule
        mgr.update_job(
            job.id,
            schedule=Schedule(kind="cron", expr="30 9 * * *").model_dump(),
        )

        scheduled = mgr._scheduler.get_jobs()
        assert any(j.id == job.id for j in scheduled)

        mgr.shutdown()

    def test_remove_job(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job()
        mgr.add_job(job)
        removed = mgr.remove_job(job.id)
        assert removed.id == job.id
        assert mgr.get_job(job.id) is None

        # Should also be unscheduled
        scheduled_ids = {j.id for j in mgr._scheduler.get_jobs()}
        assert job.id not in scheduled_ids

        mgr.shutdown()

    def test_remove_nonexistent_raises(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        with pytest.raises(KeyError, match="not found"):
            mgr.remove_job("nonexistent")


# -- Enable / Disable --


class TestCronManagerEnableDisable:
    def test_disable_job(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job("active")
        mgr.add_job(job)

        disabled = mgr.disable_job(job.id)
        assert disabled.enabled is False

        # Should be unscheduled
        scheduled_ids = {j.id for j in mgr._scheduler.get_jobs()}
        assert job.id not in scheduled_ids

        mgr.shutdown()

    def test_enable_job(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job("disabled", enabled=False)
        mgr.add_job(job)

        enabled = mgr.enable_job(job.id)
        assert enabled.enabled is True

        # Should now be scheduled
        scheduled_ids = {j.id for j in mgr._scheduler.get_jobs()}
        assert job.id in scheduled_ids

        mgr.shutdown()

    def test_enable_already_enabled_noop(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        mgr.start()

        job = _make_job("active")
        mgr.add_job(job)

        enabled = mgr.enable_job(job.id)
        assert enabled.enabled is True

        mgr.shutdown()


# -- Trigger / Execute --


class TestCronManagerTrigger:
    def test_trigger_job_calls_executor(self, tmp_path):
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job()
        store.add(job)

        mgr.trigger_job(job.id)

        executor.assert_called_once_with(job)

    def test_trigger_records_success_run(self, tmp_path):
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job()
        store.add(job)

        mgr.trigger_job(job.id)

        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS
        assert runs[0].error is None

    def test_trigger_records_failure_run(self, tmp_path):
        store = _make_store(tmp_path)
        executor = MagicMock(side_effect=RuntimeError("boom"))
        mgr = CronManager(store, executor=executor)

        job = _make_job()
        store.add(job)

        mgr.trigger_job(job.id)

        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.FAILURE
        assert runs[0].error == "boom"

    def test_trigger_nonexistent_raises(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        with pytest.raises(KeyError, match="not found"):
            mgr.trigger_job("nonexistent")

    def test_trigger_without_executor_no_crash(self, tmp_path):
        """Jobs fire without an executor; run is still recorded."""
        store = _make_store(tmp_path)
        mgr = CronManager(store)  # No executor

        job = _make_job()
        store.add(job)

        mgr.trigger_job(job.id)

        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS

    def test_failure_does_not_disable_job(self, tmp_path):
        """A failed execution should NOT disable the job for future runs."""
        store = _make_store(tmp_path)
        executor = MagicMock(side_effect=RuntimeError("oops"))
        mgr = CronManager(store, executor=executor)

        job = _make_job()
        store.add(job)

        mgr.trigger_job(job.id)

        # Job should still be enabled
        assert mgr.get_job(job.id).enabled is True


# -- One-shot auto-delete --


class TestOneShotAutoDelete:
    def test_at_job_auto_deleted_on_success(self, tmp_path):
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "reminder",
            schedule=Schedule(kind="at", expr="2026-03-01T09:00:00-07:00"),
        )
        store.add(job)

        mgr._execute_job(job)

        # Job should be removed from the store
        assert store.get(job.id) is None

        # But the run record should still exist
        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS

    def test_at_job_not_deleted_on_failure(self, tmp_path):
        store = _make_store(tmp_path)
        executor = MagicMock(side_effect=RuntimeError("fail"))
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "reminder",
            schedule=Schedule(kind="at", expr="2026-03-01T09:00:00-07:00"),
        )
        store.add(job)

        mgr._execute_job(job)

        # Job should still exist (failure doesn't trigger auto-delete)
        assert store.get(job.id) is not None

    def test_cron_job_not_auto_deleted(self, tmp_path):
        """Recurring cron jobs should never auto-delete."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job("recurring")  # default is cron schedule
        store.add(job)

        mgr._execute_job(job)

        # Job should still exist
        assert store.get(job.id) is not None

    def test_every_job_not_auto_deleted(self, tmp_path):
        """Interval jobs should never auto-delete."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "interval",
            schedule=Schedule(kind="every", expr="60"),
        )
        store.add(job)

        mgr._execute_job(job)
        assert store.get(job.id) is not None


# -- Run history via manager --


class TestCronManagerRunHistory:
    def test_get_runs_after_trigger(self, tmp_path):
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job()
        store.add(job)

        mgr.trigger_job(job.id)
        mgr.trigger_job(job.id)

        runs = mgr.get_runs(job.id)
        assert len(runs) == 2

    def test_get_runs_empty(self, tmp_path):
        store = _make_store(tmp_path)
        mgr = CronManager(store)
        assert mgr.get_runs("nonexistent") == []


# -- Scheduler firing (integration-style) --


class TestCronManagerSchedulerFiring:
    def test_interval_job_fires(self, tmp_path):
        """An 'every' job with a 1-second interval should fire at least once."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "fast interval",
            schedule=Schedule(kind="every", expr="1"),
        )
        mgr.add_job(job)
        mgr.start()

        # Wait for the job to fire
        time.sleep(2.5)

        mgr.shutdown()

        assert executor.call_count >= 1
        runs = store.get_runs(job.id)
        assert len(runs) >= 1
        assert all(r.status == RunStatus.SUCCESS for r in runs)

    def test_disabled_job_does_not_fire(self, tmp_path):
        """A disabled job should not fire even if its schedule matches."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "disabled",
            schedule=Schedule(kind="every", expr="1"),
            enabled=False,
        )
        mgr.add_job(job)
        mgr.start()

        time.sleep(2)

        mgr.shutdown()

        executor.assert_not_called()

    def test_two_jobs_both_fire(self, tmp_path):
        """Two jobs scheduled at the same interval should both run."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        j1 = _make_job("job A", schedule=Schedule(kind="every", expr="1"))
        j2 = _make_job("job B", schedule=Schedule(kind="every", expr="1"))
        mgr.add_job(j1)
        mgr.add_job(j2)
        mgr.start()

        time.sleep(2.5)

        mgr.shutdown()

        # Both jobs should have fired at least once
        j1_runs = store.get_runs(j1.id)
        j2_runs = store.get_runs(j2.id)
        assert len(j1_runs) >= 1
        assert len(j2_runs) >= 1
