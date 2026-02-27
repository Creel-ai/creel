"""JSON file persistence for cron jobs and run history."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from creel import paths
from creel.cron.models import CronJob, RunRecord

logger = logging.getLogger(__name__)

DEFAULT_MAX_RUNS_PER_JOB = 50


class JobStore:
    """Persistent storage for cron jobs and run history.

    Jobs are stored in a single JSON file as a list of CronJob dicts.
    Run history is stored in a separate JSON file as a dict mapping
    job_id -> list of RunRecord dicts, capped at max_runs_per_job.
    """

    def __init__(
        self,
        jobs_path: Path | str | None = None,
        runs_path: Path | str | None = None,
        max_runs_per_job: int = DEFAULT_MAX_RUNS_PER_JOB,
    ) -> None:
        if jobs_path is None:
            jobs_path = paths.cron_dir() / "jobs.json"
        if runs_path is None:
            runs_path = paths.cron_dir() / "runs.json"
        self._jobs_path = Path(jobs_path)
        self._runs_path = Path(runs_path)
        self._max_runs_per_job = max_runs_per_job
        self._lock = threading.Lock()

        self._jobs: dict[str, CronJob] = {}
        self._runs: dict[str, list[RunRecord]] = {}

        self.load()

    # -- Public API: jobs --

    def add(self, job: CronJob) -> CronJob:
        """Add a new job. Raises ValueError if ID already exists."""
        with self._lock:
            if job.id in self._jobs:
                raise ValueError(f"Job with id '{job.id}' already exists")
            self._jobs[job.id] = job
            self._save_jobs()
            return job

    def get(self, job_id: str) -> CronJob | None:
        """Get a job by ID, or None if not found."""
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[CronJob]:
        """Return all jobs, ordered by created_at."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at)

    def update(self, job_id: str, **fields: object) -> CronJob:
        """Update fields on an existing job. Returns the updated job.

        Raises KeyError if job not found.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job '{job_id}' not found")

            data = job.model_dump()
            for key, value in fields.items():
                if key == "id":
                    raise ValueError("Cannot change job ID")
                if key not in data:
                    raise ValueError(f"Unknown field '{key}'")
                data[key] = value

            from creel.cron.models import now_iso

            data["updated_at"] = now_iso()
            updated = CronJob(**data)
            self._jobs[job_id] = updated
            self._save_jobs()
            return updated

    def remove(self, job_id: str, *, keep_history: bool = False) -> CronJob:
        """Remove a job by ID. Returns the removed job.

        Args:
            job_id: The job to remove.
            keep_history: If True, preserve run history for the job.

        Raises KeyError if job not found.
        """
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job is None:
                raise KeyError(f"Job '{job_id}' not found")
            self._save_jobs()
            if not keep_history:
                self._runs.pop(job_id, None)
                self._save_runs()
            return job

    # -- Public API: run history --

    def add_run(self, record: RunRecord) -> RunRecord:
        """Append a run record, capping history at max_runs_per_job."""
        with self._lock:
            runs = self._runs.setdefault(record.job_id, [])
            runs.append(record)
            # Keep only the most recent N runs
            if len(runs) > self._max_runs_per_job:
                self._runs[record.job_id] = runs[-self._max_runs_per_job :]
            self._save_runs()
            return record

    def get_runs(self, job_id: str) -> list[RunRecord]:
        """Get run history for a job, ordered oldest first."""
        with self._lock:
            return list(self._runs.get(job_id, []))

    # -- Persistence --

    def load(self) -> None:
        """Load jobs and runs from disk. Tolerates corrupt files."""
        with self._lock:
            self._jobs = {}
            self._runs = {}
            self._load_jobs()
            self._load_runs()

    def save(self) -> None:
        """Write both jobs and runs to disk."""
        with self._lock:
            self._save_jobs()
            self._save_runs()

    def _load_jobs(self) -> None:
        if not self._jobs_path.exists():
            return
        try:
            data = json.loads(self._jobs_path.read_text())
            for item in data:
                job = CronJob(**item)
                self._jobs[job.id] = job
        except Exception:
            logger.exception(
                "Failed to load cron jobs from %s — starting with empty job list",
                self._jobs_path,
            )

    def _load_runs(self) -> None:
        if not self._runs_path.exists():
            return
        try:
            data = json.loads(self._runs_path.read_text())
            for job_id, records in data.items():
                self._runs[job_id] = [RunRecord(**r) for r in records]
        except Exception:
            logger.exception(
                "Failed to load run history from %s — starting with empty history",
                self._runs_path,
            )

    def _save_jobs(self) -> None:
        self._jobs_path.parent.mkdir(parents=True, exist_ok=True)
        data = [job.model_dump() for job in self._jobs.values()]
        self._atomic_write(self._jobs_path, json.dumps(data, indent=2) + "\n")

    def _save_runs(self) -> None:
        self._runs_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            job_id: [r.model_dump() for r in records]
            for job_id, records in self._runs.items()
        }
        self._atomic_write(self._runs_path, json.dumps(data, indent=2) + "\n")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write content to a file atomically via temp file + rename."""
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
