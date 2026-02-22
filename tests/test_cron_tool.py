"""Tests for the cron agent tool (list/add/update/remove/run/runs actions)."""

from __future__ import annotations

import json
from pathlib import Path

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
from taskrunner.cron.tool import CRON_TOOL_DEFINITION, handle_cron_tool


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


def _call(store: JobStore, **tool_input) -> dict:
    """Call handle_cron_tool and parse the JSON result."""
    raw = handle_cron_tool(tool_input, store)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Tool definition schema
# ---------------------------------------------------------------------------


class TestCronToolDefinition:
    def test_has_name(self) -> None:
        assert CRON_TOOL_DEFINITION["name"] == "cron"

    def test_has_description(self) -> None:
        assert "scheduled" in CRON_TOOL_DEFINITION["description"].lower()

    def test_action_is_required(self) -> None:
        schema = CRON_TOOL_DEFINITION["input_schema"]
        assert "action" in schema["required"]

    def test_action_enum(self) -> None:
        props = CRON_TOOL_DEFINITION["input_schema"]["properties"]
        assert set(props["action"]["enum"]) == {
            "list", "add", "update", "remove", "run", "runs",
        }

    def test_has_key_properties(self) -> None:
        props = CRON_TOOL_DEFINITION["input_schema"]["properties"]
        expected = {
            "action", "job_id", "name", "schedule_kind", "schedule_expr",
            "tz", "message", "target", "payload_kind", "model",
            "delivery_mode", "delivery_channel", "delivery_url", "enabled",
        }
        assert expected <= set(props.keys())


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


class TestUnknownAction:
    def test_unknown_action_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="explode")
        assert "error" in result
        assert "Unknown action" in result["error"]

    def test_missing_action_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store)
        assert "error" in result


# ---------------------------------------------------------------------------
# list action
# ---------------------------------------------------------------------------


class TestActionList:
    def test_empty_list(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="list")
        assert result["count"] == 0
        assert result["jobs"] == []

    def test_list_with_jobs(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add(_make_job("Job A"))
        store.add(_make_job("Job B"))

        result = _call(store, action="list")
        assert result["count"] == 2
        names = {j["name"] for j in result["jobs"]}
        assert names == {"Job A", "Job B"}

    def test_list_includes_schedule_info(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add(_make_job("Daily", schedule=Schedule(kind="cron", expr="0 8 * * *")))

        result = _call(store, action="list")
        job = result["jobs"][0]
        assert "cron" in job["schedule"]
        assert "0 8 * * *" in job["schedule"]

    def test_list_includes_enabled_status(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add(_make_job("Active", enabled=True))
        store.add(_make_job("Paused", enabled=False))

        result = _call(store, action="list")
        statuses = {j["name"]: j["enabled"] for j in result["jobs"]}
        assert statuses["Active"] is True
        assert statuses["Paused"] is False


# ---------------------------------------------------------------------------
# add action
# ---------------------------------------------------------------------------


class TestActionAdd:
    def test_add_cron_job(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Morning briefing",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="Summarize emails",
        )
        assert result["status"] == "created"
        assert result["job"]["name"] == "Morning briefing"
        assert store.list()[0].name == "Morning briefing"

    def test_add_every_job(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Health check",
            schedule_kind="every",
            schedule_expr="300",
            message="Check system health",
        )
        assert result["status"] == "created"
        job = store.list()[0]
        assert job.schedule.kind == "every"
        assert job.schedule.expr == "300"

    def test_add_at_job(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Reminder",
            schedule_kind="at",
            schedule_expr="2026-03-01T09:00:00",
            message="Time to go",
        )
        assert result["status"] == "created"
        job = store.list()[0]
        assert job.schedule.kind == "at"

    def test_add_with_timezone(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Denver time",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="Good morning",
            tz="America/Denver",
        )
        assert result["status"] == "created"
        assert store.list()[0].schedule.tz == "America/Denver"

    def test_add_system_event_forces_main(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Event",
            schedule_kind="cron",
            schedule_expr="0 9 * * *",
            message="Check email",
            payload_kind="systemEvent",
            target="isolated",  # should be overridden
        )
        assert result["status"] == "created"
        job = store.list()[0]
        assert job.target == "main"
        assert job.payload.kind == "systemEvent"

    def test_add_with_model_override(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Heavy task",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="Analyze data",
            model="claude-opus-4-20250514",
        )
        assert result["status"] == "created"
        assert store.list()[0].payload.model == "claude-opus-4-20250514"

    def test_add_with_announce_delivery(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Announced",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="hello",
            delivery_mode="announce",
            delivery_channel="whatsapp",
        )
        assert result["status"] == "created"
        job = store.list()[0]
        assert job.delivery.mode == "announce"
        assert job.delivery.channel == "whatsapp"

    def test_add_with_webhook_delivery(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Webhook",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="hello",
            delivery_mode="webhook",
            delivery_url="https://example.com/hook",
        )
        assert result["status"] == "created"
        job = store.list()[0]
        assert job.delivery.mode == "webhook"
        assert job.delivery.url == "https://example.com/hook"

    def test_add_missing_name_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="do stuff",
        )
        assert "error" in result
        assert "name" in result["error"]

    def test_add_missing_schedule_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="No schedule",
            message="do stuff",
        )
        assert "error" in result
        assert "schedule" in result["error"]

    def test_add_missing_message_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="No message",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
        )
        assert "error" in result
        assert "message" in result["error"]

    def test_add_invalid_cron_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Bad cron",
            schedule_kind="cron",
            schedule_expr="not a cron",
            message="do stuff",
        )
        assert "error" in result

    def test_add_persists_to_disk(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _call(
            store,
            action="add",
            name="Persistent",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="do stuff",
        )

        # Reload from disk
        store2 = _make_store(tmp_path)
        assert len(store2.list()) == 1
        assert store2.list()[0].name == "Persistent"

    def test_add_default_delivery_is_none(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _call(
            store,
            action="add",
            name="Default delivery",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="hello",
        )
        job = store.list()[0]
        assert job.delivery.mode == "none"

    def test_add_default_target_is_isolated(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _call(
            store,
            action="add",
            name="Default target",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="hello",
        )
        job = store.list()[0]
        assert job.target == "isolated"


# ---------------------------------------------------------------------------
# update action
# ---------------------------------------------------------------------------


class TestActionUpdate:
    def test_update_name(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Original")
        store.add(job)

        result = _call(store, action="update", job_id=job.id, name="Renamed")
        assert result["status"] == "updated"
        assert result["job"]["name"] == "Renamed"
        assert store.get(job.id).name == "Renamed"

    def test_update_enabled(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Active", enabled=True)
        store.add(job)

        result = _call(store, action="update", job_id=job.id, enabled=False)
        assert result["status"] == "updated"
        assert store.get(job.id).enabled is False

    def test_update_schedule(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Reschedule")
        store.add(job)

        result = _call(
            store,
            action="update",
            job_id=job.id,
            schedule_kind="every",
            schedule_expr="600",
        )
        assert result["status"] == "updated"
        updated = store.get(job.id)
        assert updated.schedule.kind == "every"
        assert updated.schedule.expr == "600"

    def test_update_no_changes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Unchanged")
        store.add(job)

        result = _call(store, action="update", job_id=job.id)
        assert result["status"] == "no_changes"

    def test_update_missing_job_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="update", name="Oops")
        assert "error" in result
        assert "job_id" in result["error"]

    def test_update_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="update", job_id="nonexistent", name="X")
        assert "error" in result
        assert "not found" in result["error"]

    def test_update_preserves_timezone(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job(
            "TZ job",
            schedule=Schedule(kind="cron", expr="0 8 * * *", tz="America/Denver"),
        )
        store.add(job)

        result = _call(
            store,
            action="update",
            job_id=job.id,
            schedule_kind="cron",
            schedule_expr="30 9 * * *",
        )
        assert result["status"] == "updated"
        assert store.get(job.id).schedule.tz == "America/Denver"


# ---------------------------------------------------------------------------
# remove action
# ---------------------------------------------------------------------------


class TestActionRemove:
    def test_remove_existing(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Deletable")
        store.add(job)

        result = _call(store, action="remove", job_id=job.id)
        assert result["status"] == "removed"
        assert result["job"]["name"] == "Deletable"
        assert store.get(job.id) is None

    def test_remove_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="remove", job_id="nonexistent")
        assert "error" in result
        assert "not found" in result["error"]

    def test_remove_missing_job_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="remove")
        assert "error" in result
        assert "job_id" in result["error"]

    def test_remove_persists(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("To delete")
        store.add(job)

        _call(store, action="remove", job_id=job.id)

        store2 = _make_store(tmp_path)
        assert store2.get(job.id) is None


# ---------------------------------------------------------------------------
# run action
# ---------------------------------------------------------------------------


class TestActionRun:
    def test_run_existing_job(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Triggerable")
        store.add(job)

        result = _call(store, action="run", job_id=job.id)
        assert result["status"] == "triggered"
        assert result["job"]["name"] == "Triggerable"

    def test_run_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="run", job_id="nonexistent")
        assert "error" in result
        assert "not found" in result["error"]

    def test_run_missing_job_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="run")
        assert "error" in result
        assert "job_id" in result["error"]


# ---------------------------------------------------------------------------
# runs action
# ---------------------------------------------------------------------------


class TestActionRuns:
    def test_no_runs(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("No runs")
        store.add(job)

        result = _call(store, action="runs", job_id=job.id)
        assert result["count"] == 0
        assert result["runs"] == []
        assert result["job_name"] == "No runs"

    def test_with_runs(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Has runs")
        store.add(job)

        store.add_run(RunRecord(
            job_id=job.id,
            started_at="2026-01-15T08:00:00+00:00",
            ended_at="2026-01-15T08:00:05+00:00",
            status=RunStatus.SUCCESS,
        ))
        store.add_run(RunRecord(
            job_id=job.id,
            started_at="2026-01-16T08:00:00+00:00",
            ended_at="2026-01-16T08:00:03+00:00",
            status=RunStatus.FAILURE,
            error="timeout",
        ))

        result = _call(store, action="runs", job_id=job.id)
        assert result["count"] == 2
        assert result["runs"][0]["status"] == "success"
        assert result["runs"][1]["status"] == "failure"
        assert result["runs"][1]["error"] == "timeout"

    def test_runs_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="runs", job_id="nonexistent")
        assert "error" in result
        assert "not found" in result["error"]

    def test_runs_missing_job_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(store, action="runs")
        assert "error" in result
        assert "job_id" in result["error"]

    def test_success_runs_no_error_field(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _make_job("Clean runs")
        store.add(job)

        store.add_run(RunRecord(
            job_id=job.id,
            started_at="2026-01-15T08:00:00+00:00",
            ended_at="2026-01-15T08:00:05+00:00",
            status=RunStatus.SUCCESS,
        ))

        result = _call(store, action="runs", job_id=job.id)
        assert "error" not in result["runs"][0]


# ---------------------------------------------------------------------------
# Integration: execute_tool_call dispatches to cron
# ---------------------------------------------------------------------------


class TestExecuteToolCallCronDispatch:
    def test_dispatch_with_cron_store(self, tmp_path: Path) -> None:
        from taskrunner.tools import execute_tool_call

        store = _make_store(tmp_path)
        store.add(_make_job("Dispatched"))

        result_str = execute_tool_call(
            tool_name="cron",
            tool_input={"action": "list"},
            tools_config={},
            cron_store=store,
        )
        result = json.loads(result_str)
        assert result["count"] == 1
        assert result["jobs"][0]["name"] == "Dispatched"

    def test_dispatch_without_cron_store_raises(self, tmp_path: Path) -> None:
        from taskrunner.tools import execute_tool_call

        with pytest.raises(ValueError, match="Unknown tool"):
            execute_tool_call(
                tool_name="cron",
                tool_input={"action": "list"},
                tools_config={},
                cron_store=None,
            )

    def test_add_via_dispatch(self, tmp_path: Path) -> None:
        from taskrunner.tools import execute_tool_call

        store = _make_store(tmp_path)
        result_str = execute_tool_call(
            tool_name="cron",
            tool_input={
                "action": "add",
                "name": "Via dispatch",
                "schedule_kind": "cron",
                "schedule_expr": "0 8 * * *",
                "message": "hello",
            },
            tools_config={},
            cron_store=store,
        )
        result = json.loads(result_str)
        assert result["status"] == "created"
        assert len(store.list()) == 1


# ---------------------------------------------------------------------------
# Integration: build_tool_definitions includes cron
# ---------------------------------------------------------------------------


class TestBuildToolDefinitionsCron:
    def test_includes_cron_when_flagged(self) -> None:
        from taskrunner.tools import build_tool_definitions

        defs = build_tool_definitions({}, include_cron_tools=True)
        names = [d["name"] for d in defs]
        assert "cron" in names

    def test_excludes_cron_by_default(self) -> None:
        from taskrunner.tools import build_tool_definitions

        defs = build_tool_definitions({})
        names = [d["name"] for d in defs]
        assert "cron" not in names

    def test_cron_with_other_builtins(self) -> None:
        from taskrunner.tools import build_tool_definitions

        defs = build_tool_definitions(
            {},
            include_memory_tools=True,
            include_cron_tools=True,
        )
        names = [d["name"] for d in defs]
        assert "cron" in names
        assert "remember" in names


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


class TestErrorResilience:
    def test_invalid_schedule_expr_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Bad expr",
            schedule_kind="every",
            schedule_expr="not_a_number",
            message="do stuff",
        )
        assert "error" in result

    def test_invalid_at_expr_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="Bad at",
            schedule_kind="at",
            schedule_expr="tomorrow morning",
            message="do stuff",
        )
        assert "error" in result

    def test_announce_without_channel_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="No channel",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="hello",
            delivery_mode="announce",
        )
        assert "error" in result

    def test_webhook_without_url_returns_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _call(
            store,
            action="add",
            name="No url",
            schedule_kind="cron",
            schedule_expr="0 8 * * *",
            message="hello",
            delivery_mode="webhook",
        )
        assert "error" in result
