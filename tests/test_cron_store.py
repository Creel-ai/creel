"""Tests for cron JobStore persistence."""

import json

import pytest

from taskrunner.cron.models import (
    CronJob,
    Delivery,
    Payload,
    RunRecord,
    RunStatus,
    Schedule,
)
from taskrunner.cron.store import JobStore


def _make_job(name: str = "test job", **kwargs) -> CronJob:
    """Helper to create a CronJob with sensible defaults."""
    defaults = dict(
        name=name,
        schedule=Schedule(kind="cron", expr="0 8 * * *"),
        payload=Payload(message="do stuff"),
    )
    defaults.update(kwargs)
    return CronJob(**defaults)


def _make_run(job_id: str, status: RunStatus = RunStatus.SUCCESS) -> RunRecord:
    return RunRecord(
        job_id=job_id,
        started_at="2026-02-21T08:00:00+00:00",
        ended_at="2026-02-21T08:00:05+00:00",
        status=status,
    )


class TestJobStoreBasics:
    def test_add_and_get(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)

        retrieved = store.get(job.id)
        assert retrieved is not None
        assert retrieved.id == job.id
        assert retrieved.name == "test job"

    def test_get_nonexistent(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        assert store.get("nonexistent") is None

    def test_add_duplicate_raises(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)

        with pytest.raises(ValueError, match="already exists"):
            store.add(job)

    def test_list_empty(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        assert store.list() == []

    def test_list_returns_sorted_by_created_at(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        j1 = _make_job("first", created_at="2026-01-01T00:00:00+00:00")
        j2 = _make_job("second", created_at="2026-02-01T00:00:00+00:00")
        j3 = _make_job("third", created_at="2026-01-15T00:00:00+00:00")
        # Add out of order
        store.add(j2)
        store.add(j1)
        store.add(j3)

        result = store.list()
        assert [j.name for j in result] == ["first", "third", "second"]

    def test_remove(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)
        removed = store.remove(job.id)
        assert removed.id == job.id
        assert store.get(job.id) is None

    def test_remove_nonexistent_raises(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        with pytest.raises(KeyError, match="not found"):
            store.remove("nonexistent")


class TestJobStoreUpdate:
    def test_update_name(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job("original")
        store.add(job)
        updated = store.update(job.id, name="renamed")
        assert updated.name == "renamed"
        assert store.get(job.id).name == "renamed"

    def test_update_enabled(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)
        updated = store.update(job.id, enabled=False)
        assert updated.enabled is False

    def test_update_sets_updated_at(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job(created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
        store.add(job)
        updated = store.update(job.id, name="changed")
        assert updated.updated_at > "2026-01-01T00:00:00+00:00"

    def test_update_nonexistent_raises(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        with pytest.raises(KeyError, match="not found"):
            store.update("nonexistent", name="nope")

    def test_update_id_raises(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)
        with pytest.raises(ValueError, match="Cannot change job ID"):
            store.update(job.id, id="new-id")

    def test_update_unknown_field_raises(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)
        with pytest.raises(ValueError, match="Unknown field"):
            store.update(job.id, nonexistent_field="value")


class TestJobStorePersistence:
    def test_roundtrip(self, tmp_path):
        jobs_path = tmp_path / "jobs.json"
        runs_path = tmp_path / "runs.json"

        store1 = JobStore(jobs_path=jobs_path, runs_path=runs_path)
        job = _make_job("persistent")
        store1.add(job)

        # Create a new store instance from the same files
        store2 = JobStore(jobs_path=jobs_path, runs_path=runs_path)
        retrieved = store2.get(job.id)
        assert retrieved is not None
        assert retrieved.name == "persistent"

    def test_creates_parent_dirs(self, tmp_path):
        deep_path = tmp_path / "a" / "b" / "c" / "jobs.json"
        runs_path = tmp_path / "a" / "b" / "c" / "runs.json"
        store = JobStore(jobs_path=deep_path, runs_path=runs_path)
        store.add(_make_job())
        assert deep_path.exists()

    def test_corrupt_jobs_file_starts_empty(self, tmp_path):
        jobs_path = tmp_path / "jobs.json"
        runs_path = tmp_path / "runs.json"
        jobs_path.write_text("not valid json {{{")

        store = JobStore(jobs_path=jobs_path, runs_path=runs_path)
        assert store.list() == []

    def test_corrupt_runs_file_starts_empty(self, tmp_path):
        jobs_path = tmp_path / "jobs.json"
        runs_path = tmp_path / "runs.json"
        runs_path.write_text("not valid json {{{")

        store = JobStore(jobs_path=jobs_path, runs_path=runs_path)
        assert store.get_runs("any") == []


class TestRunHistory:
    def test_add_and_get_runs(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)

        run = _make_run(job.id)
        store.add_run(run)

        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].job_id == job.id
        assert runs[0].status == RunStatus.SUCCESS

    def test_get_runs_empty(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        assert store.get_runs("nonexistent") == []

    def test_runs_capped_at_max(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
            max_runs_per_job=3,
        )
        job = _make_job()
        store.add(job)

        for i in range(5):
            store.add_run(RunRecord(
                job_id=job.id,
                started_at=f"2026-02-21T08:0{i}:00+00:00",
                ended_at=f"2026-02-21T08:0{i}:05+00:00",
                status=RunStatus.SUCCESS,
            ))

        runs = store.get_runs(job.id)
        assert len(runs) == 3
        # Should keep the most recent
        assert runs[0].started_at == "2026-02-21T08:02:00+00:00"
        assert runs[2].started_at == "2026-02-21T08:04:00+00:00"

    def test_runs_persist_across_reload(self, tmp_path):
        jobs_path = tmp_path / "jobs.json"
        runs_path = tmp_path / "runs.json"

        store1 = JobStore(jobs_path=jobs_path, runs_path=runs_path)
        job = _make_job()
        store1.add(job)
        store1.add_run(_make_run(job.id))

        store2 = JobStore(jobs_path=jobs_path, runs_path=runs_path)
        runs = store2.get_runs(job.id)
        assert len(runs) == 1

    def test_remove_job_cleans_up_runs(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)
        store.add_run(_make_run(job.id))

        store.remove(job.id)
        assert store.get_runs(job.id) == []

    def test_failure_run_with_error(self, tmp_path):
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=tmp_path / "runs.json",
        )
        job = _make_job()
        store.add(job)

        run = RunRecord(
            job_id=job.id,
            started_at="2026-02-21T08:00:00+00:00",
            ended_at="2026-02-21T08:00:02+00:00",
            status=RunStatus.FAILURE,
            error="timeout",
        )
        store.add_run(run)

        runs = store.get_runs(job.id)
        assert runs[0].status == RunStatus.FAILURE
        assert runs[0].error == "timeout"


class TestJobStoreJsonFormat:
    """Verify the on-disk JSON format is what we expect."""

    def test_jobs_file_is_list_of_dicts(self, tmp_path):
        jobs_path = tmp_path / "jobs.json"
        store = JobStore(
            jobs_path=jobs_path,
            runs_path=tmp_path / "runs.json",
        )
        store.add(_make_job("alpha"))
        store.add(_make_job("beta"))

        raw = json.loads(jobs_path.read_text())
        assert isinstance(raw, list)
        assert len(raw) == 2
        assert all(isinstance(item, dict) for item in raw)
        names = {item["name"] for item in raw}
        assert names == {"alpha", "beta"}

    def test_runs_file_is_dict_of_lists(self, tmp_path):
        runs_path = tmp_path / "runs.json"
        store = JobStore(
            jobs_path=tmp_path / "jobs.json",
            runs_path=runs_path,
        )
        job = _make_job()
        store.add(job)
        store.add_run(_make_run(job.id))

        raw = json.loads(runs_path.read_text())
        assert isinstance(raw, dict)
        assert job.id in raw
        assert isinstance(raw[job.id], list)
        assert len(raw[job.id]) == 1
