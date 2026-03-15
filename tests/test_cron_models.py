"""Tests for cron data models."""

import pytest
from pydantic import ValidationError

from creel.cron.models import (
    CronJob,
    Delivery,
    Payload,
    RunRecord,
    RunStatus,
    Schedule,
)

# --- Schedule ---


class TestSchedule:
    def test_cron_valid(self):
        s = Schedule(kind="cron", expr="0 8 * * *")
        assert s.kind == "cron"
        assert s.expr == "0 8 * * *"
        assert s.tz == "UTC"

    def test_cron_with_timezone(self):
        s = Schedule(kind="cron", expr="30 9 * * 1-5", tz="America/Denver")
        assert s.tz == "America/Denver"

    def test_cron_invalid_parts(self):
        with pytest.raises(ValidationError, match="5 parts"):
            Schedule(kind="cron", expr="0 8 * *")

    def test_every_valid(self):
        s = Schedule(kind="every", expr="300")
        assert s.kind == "every"
        assert s.expr == "300"

    def test_every_invalid_not_int(self):
        with pytest.raises(ValidationError, match="integer"):
            Schedule(kind="every", expr="five")

    def test_every_invalid_zero(self):
        with pytest.raises(ValidationError, match=">= 1"):
            Schedule(kind="every", expr="0")

    def test_at_valid(self):
        s = Schedule(kind="at", expr="2026-03-01T09:00:00-07:00")
        assert s.kind == "at"

    def test_at_invalid_format(self):
        with pytest.raises(ValidationError, match="ISO 8601"):
            Schedule(kind="at", expr="next tuesday")


# --- Payload ---


class TestPayload:
    def test_agent_turn_default(self):
        p = Payload(message="do stuff")
        assert p.kind == "agentTurn"
        assert p.message == "do stuff"
        assert p.model is None
        assert p.timeout_seconds == 120

    def test_system_event(self):
        p = Payload(kind="systemEvent", message="reminder: standup")
        assert p.kind == "systemEvent"

    def test_with_model_override(self):
        p = Payload(message="summarize", model="claude-haiku-4-5")
        assert p.model == "claude-haiku-4-5"


# --- Delivery ---


class TestDelivery:
    def test_announce_with_channel(self):
        d = Delivery(mode="announce", channel="whatsapp")
        assert d.mode == "announce"
        assert d.channel == "whatsapp"

    def test_announce_missing_channel(self):
        with pytest.raises(ValidationError, match="channel is required"):
            Delivery(mode="announce")

    def test_webhook_with_url(self):
        d = Delivery(mode="webhook", url="https://hooks.example.com/cron")
        assert d.url == "https://hooks.example.com/cron"

    def test_webhook_missing_url(self):
        with pytest.raises(ValidationError, match="url is required"):
            Delivery(mode="webhook")

    def test_none_mode(self):
        d = Delivery(mode="none")
        assert d.mode == "none"

    def test_best_effort_default(self):
        d = Delivery(mode="none")
        assert d.best_effort is True


# --- RunRecord ---


class TestRunRecord:
    def test_success_record(self):
        r = RunRecord(
            job_id="abc123",
            started_at="2026-02-21T08:00:00+00:00",
            ended_at="2026-02-21T08:00:05+00:00",
            status=RunStatus.SUCCESS,
        )
        assert r.status == RunStatus.SUCCESS
        assert r.error is None

    def test_failure_record(self):
        r = RunRecord(
            job_id="abc123",
            started_at="2026-02-21T08:00:00+00:00",
            ended_at="2026-02-21T08:00:02+00:00",
            status=RunStatus.FAILURE,
            error="LLM call timed out",
        )
        assert r.status == RunStatus.FAILURE
        assert r.error == "LLM call timed out"

    def test_invalid_timestamp(self):
        with pytest.raises(ValidationError, match="ISO 8601"):
            RunRecord(
                job_id="abc123",
                started_at="not-a-date",
                status=RunStatus.SUCCESS,
            )


# --- CronJob ---


class TestCronJob:
    def test_minimal_job(self):
        job = CronJob(
            name="Morning briefing",
            schedule=Schedule(kind="cron", expr="0 8 * * *"),
            payload=Payload(message="Summarize overnight emails."),
        )
        assert job.name == "Morning briefing"
        assert len(job.id) == 12
        assert job.target == "isolated"
        assert job.enabled is True
        assert job.delivery.mode == "none"

    def test_full_job(self):
        job = CronJob(
            name="Email digest",
            schedule=Schedule(kind="cron", expr="0 8 * * *", tz="America/Denver"),
            target="isolated",
            payload=Payload(
                kind="agentTurn",
                message="Summarize overnight emails and today's calendar.",
                model="claude-sonnet-4-6",
                timeout_seconds=120,
            ),
            delivery=Delivery(mode="announce", channel="whatsapp"),
            enabled=True,
        )
        assert job.delivery.mode == "announce"
        assert job.delivery.channel == "whatsapp"
        assert job.payload.model == "claude-sonnet-4-6"

    def test_one_shot_job(self):
        job = CronJob(
            name="Reminder",
            schedule=Schedule(kind="at", expr="2026-03-01T17:00:00-07:00"),
            target="main",
            payload=Payload(kind="systemEvent", message="Time for standup!"),
        )
        assert job.schedule.kind == "at"
        assert job.target == "main"

    def test_interval_job(self):
        job = CronJob(
            name="Health check",
            schedule=Schedule(kind="every", expr="60"),
            payload=Payload(message="Check system health."),
        )
        assert job.schedule.kind == "every"
        assert job.schedule.expr == "60"

    def test_ids_are_unique(self):
        j1 = CronJob(
            name="A",
            schedule=Schedule(kind="every", expr="60"),
            payload=Payload(message="a"),
        )
        j2 = CronJob(
            name="B",
            schedule=Schedule(kind="every", expr="60"),
            payload=Payload(message="b"),
        )
        assert j1.id != j2.id

    def test_serialization_roundtrip(self):
        job = CronJob(
            name="Roundtrip test",
            schedule=Schedule(kind="cron", expr="0 8 * * *"),
            payload=Payload(message="test"),
            delivery=Delivery(mode="none"),
        )
        data = job.model_dump()
        restored = CronJob(**data)
        assert restored.id == job.id
        assert restored.name == job.name
        assert restored.schedule.kind == "cron"
        assert restored.payload.message == "test"

    def test_json_roundtrip(self):
        job = CronJob(
            name="JSON test",
            schedule=Schedule(kind="at", expr="2026-06-01T12:00:00+00:00"),
            payload=Payload(message="test"),
        )
        json_str = job.model_dump_json()
        restored = CronJob.model_validate_json(json_str)
        assert restored.id == job.id
        assert restored.schedule.expr == "2026-06-01T12:00:00+00:00"
