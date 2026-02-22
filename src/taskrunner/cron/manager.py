"""CronManager — wraps JobStore + APScheduler for dynamic job management."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from taskrunner.cron.models import (
    CronJob,
    Delivery,
    Payload,
    RunRecord,
    RunStatus,
    Schedule,
    now_iso,
)
from taskrunner.cron.store import JobStore

logger = logging.getLogger(__name__)

# Type for the executor callback: receives a CronJob, returns None.
# The executor is responsible for actually running the job payload.
JobExecutor = Callable[[CronJob], None]


def _make_trigger(schedule: Schedule) -> CronTrigger | IntervalTrigger | DateTrigger:
    """Convert a Schedule model into an APScheduler trigger."""
    if schedule.kind == "cron":
        return CronTrigger.from_crontab(schedule.expr, timezone=schedule.tz)
    elif schedule.kind == "every":
        return IntervalTrigger(seconds=int(schedule.expr))
    elif schedule.kind == "at":
        run_date = datetime.fromisoformat(schedule.expr)
        return DateTrigger(run_date=run_date, timezone=schedule.tz)
    else:
        raise ValueError(f"Unknown schedule kind: {schedule.kind}")


class CronManager:
    """High-level manager for dynamic cron jobs.

    Wraps a JobStore (persistence) and APScheduler BackgroundScheduler
    (scheduling). Provides CRUD operations and trigger/enable/disable.
    """

    def __init__(
        self,
        store: JobStore,
        executor: JobExecutor | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._scheduler = BackgroundScheduler()
        # Legacy YAML jobs: loaded in-memory only, not persisted to jobs.json
        self._legacy_jobs: dict[str, CronJob] = {}

    @property
    def store(self) -> JobStore:
        return self._store

    @property
    def running(self) -> bool:
        return self._scheduler.running

    @property
    def legacy_job_count(self) -> int:
        return len(self._legacy_jobs)

    # -- Lifecycle --

    def start(self) -> None:
        """Load all enabled jobs into the scheduler and start it."""
        for job in self._store.list():
            if job.enabled:
                try:
                    self._schedule_job(job)
                except Exception:
                    logger.exception(
                        "Failed to schedule job '%s' (%s) on startup — skipping",
                        job.name, job.id,
                    )

        for job in self._legacy_jobs.values():
            if job.enabled:
                try:
                    self._schedule_job(job)
                except Exception:
                    logger.exception(
                        "Failed to schedule legacy job '%s' (%s) on startup — skipping",
                        job.name, job.id,
                    )

        self._scheduler.start()
        logger.info(
            "CronManager started with %d managed + %d legacy jobs",
            len(self._store.list()),
            len(self._legacy_jobs),
        )

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the scheduler gracefully."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("CronManager shut down")

    # -- CRUD for managed jobs --

    def add_job(self, job: CronJob) -> CronJob:
        """Add a new job to the store and schedule it if enabled.

        Raises if the job cannot be scheduled (invalid cron/timezone/etc.).
        """
        self._store.add(job)
        if job.enabled and self._scheduler.running:
            try:
                self._schedule_job(job)
            except Exception:
                # Roll back: don't leave an unschedulable job in the store
                self._store.remove(job.id)
                raise
        return job

    def get_job(self, job_id: str) -> CronJob | None:
        """Get a job by ID (managed or legacy)."""
        job = self._store.get(job_id)
        if job is not None:
            return job
        return self._legacy_jobs.get(job_id)

    def list_jobs(self) -> list[CronJob]:
        """Return all jobs (managed + legacy), sorted by created_at."""
        all_jobs = self._store.list() + sorted(
            self._legacy_jobs.values(), key=lambda j: j.created_at
        )
        return sorted(all_jobs, key=lambda j: j.created_at)

    def update_job(self, job_id: str, **fields: Any) -> CronJob:
        """Update fields on a managed job and reschedule.

        Legacy jobs cannot be updated — raises ValueError.
        """
        if job_id in self._legacy_jobs:
            raise ValueError(
                f"Job '{job_id}' is a legacy YAML job and cannot be updated. "
                "Use 'creel cron import' to convert it first."
            )
        updated = self._store.update(job_id, **fields)
        self._unschedule_job(job_id)
        if updated.enabled and self._scheduler.running:
            self._schedule_job(updated)
        return updated

    def remove_job(self, job_id: str) -> CronJob:
        """Remove a managed job from store and unschedule it.

        Legacy jobs cannot be removed — raises ValueError.
        """
        if job_id in self._legacy_jobs:
            raise ValueError(
                f"Job '{job_id}' is a legacy YAML job and cannot be removed. "
                "Use 'creel cron import' to convert it first."
            )
        self._unschedule_job(job_id)
        return self._store.remove(job_id)

    def trigger_job(self, job_id: str) -> None:
        """Trigger a job to run immediately, regardless of schedule."""
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job '{job_id}' not found")
        self._execute_job(job)

    def enable_job(self, job_id: str) -> CronJob:
        """Enable a disabled job so it resumes firing on schedule."""
        return self.update_job(job_id, enabled=True)

    def disable_job(self, job_id: str) -> CronJob:
        """Disable a job so it stops firing (without removing it)."""
        return self.update_job(job_id, enabled=False)

    def get_runs(self, job_id: str) -> list[RunRecord]:
        """Get run history for a job."""
        return self._store.get_runs(job_id)

    # -- Legacy YAML task loading --

    def load_legacy_tasks(self, tasks_dir: str | Path) -> int:
        """Load YAML task files as read-only legacy jobs.

        Returns the number of tasks loaded.
        """
        from taskrunner.models import load_all_tasks

        tasks_dir = Path(tasks_dir)
        if not tasks_dir.is_dir():
            logger.warning("Tasks directory not found: %s", tasks_dir)
            return 0

        tasks = load_all_tasks(tasks_dir)
        count = 0
        for task in tasks:
            job = CronJob(
                id=f"legacy-{task.name}",
                name=task.name,
                schedule=Schedule(kind="cron", expr=task.schedule),
                target="isolated",
                payload=Payload(
                    kind="agentTurn",
                    message=task.prompt,
                ),
                delivery=Delivery(mode="none"),
                enabled=True,
                source="yaml_import",
            )
            self._legacy_jobs[job.id] = job
            count += 1
            logger.info("Loaded legacy YAML task '%s' as job '%s'", task.name, job.id)

        return count

    # -- Internal scheduling helpers --

    def _schedule_job(self, job: CronJob) -> None:
        """Add a job to the APScheduler.

        Raises on failure so callers can handle or propagate the error.
        """
        trigger = _make_trigger(job.schedule)
        kwargs: dict[str, object] = dict(
            args=[job.id],
            id=job.id,
            name=job.name,
            replace_existing=True,
        )
        # One-shot `at` jobs: allow unlimited misfire grace so past
        # timestamps fire immediately instead of being skipped.
        if job.schedule.kind == "at":
            kwargs["misfire_grace_time"] = None
        self._scheduler.add_job(
            self._on_job_fire,
            trigger,
            **kwargs,
        )

    def _unschedule_job(self, job_id: str) -> None:
        """Remove a job from APScheduler (if scheduled)."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass  # Job may not be scheduled

    def _on_job_fire(self, job_id: str) -> None:
        """Called by APScheduler when a job's trigger fires."""
        job = self.get_job(job_id)
        if job is None:
            logger.warning("Fired job '%s' not found — skipping", job_id)
            return

        if not job.enabled:
            logger.debug("Job '%s' is disabled — skipping", job_id)
            return

        self._execute_job(job)

    def _execute_job(self, job: CronJob) -> None:
        """Run a job: call executor, record run, handle one-shot cleanup."""
        started_at = now_iso()
        status = RunStatus.SUCCESS
        error_msg: str | None = None

        try:
            if self._executor is not None:
                self._executor(job)
            else:
                logger.info(
                    "Job '%s' (%s) fired but no executor configured — skipping",
                    job.name,
                    job.id,
                )
        except Exception as exc:
            status = RunStatus.FAILURE
            error_msg = str(exc)
            logger.exception("Job '%s' (%s) execution failed", job.name, job.id)

        # Record the run
        record = RunRecord(
            job_id=job.id,
            started_at=started_at,
            ended_at=now_iso(),
            status=status,
            error=error_msg,
        )
        self._store.add_run(record)

        # Auto-delete one-shot `at` jobs after success
        if (
            job.schedule.kind == "at"
            and status == RunStatus.SUCCESS
            and job.id not in self._legacy_jobs
        ):
            try:
                self._store.remove(job.id, keep_history=True)
                logger.info(
                    "One-shot job '%s' (%s) auto-deleted after success",
                    job.name,
                    job.id,
                )
            except KeyError:
                pass  # Already removed
