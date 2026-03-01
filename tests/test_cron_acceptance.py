"""Phase 7 — Acceptance tests for the cron / scheduled jobs system.

End-to-end integration tests that verify cross-component behavior:
  - at job fires once and auto-deletes
  - every job fires repeatedly
  - past one-shot fires immediately
  - CLI add → scheduler fires → delivery routes → history recorded
  - Agent tool creates job → fires → injects into session
  - Legacy YAML tasks work alongside managed jobs
  - Edge cases: failed payload, corrupt recovery, concurrent jobs
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from taskrunner.channels.base import Channel
from taskrunner.cron.delivery import deliver
from taskrunner.cron.executor import JobExecutor
from taskrunner.cron.manager import CronManager
from taskrunner.cron.models import (
    CronJob,
    Delivery,
    Payload,
    RunRecord,
    RunStatus,
    Schedule,
)
from taskrunner.cron.store import JobStore
from taskrunner.cron.tool import handle_cron_tool
from taskrunner.session import SessionManager

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


def _future_iso(seconds: int = 2) -> str:
    """Return an ISO 8601 timestamp `seconds` in the future."""
    dt = datetime.now(UTC) + timedelta(seconds=seconds)
    return dt.isoformat()


def _past_iso(seconds: int = 60) -> str:
    """Return an ISO 8601 timestamp `seconds` in the past."""
    dt = datetime.now(UTC) - timedelta(seconds=seconds)
    return dt.isoformat()


class _StubChatServer:
    """Minimal chat-server shape for testing event injection."""

    def __init__(self, sessions_dir: Path) -> None:
        self._session_mgr = SessionManager(sessions_dir=str(sessions_dir), max_history=50)
        self._guardian = None
        self.injected_events: list[tuple[str, str]] = []

    def inject_system_event(self, sender_id: str, text: str) -> None:
        self.injected_events.append((sender_id, text))
        self._session_mgr.add_user_message(sender_id, text)


def _poll_until(predicate, timeout=5, interval=0.1):
    """Poll until predicate() is truthy or timeout is reached."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)


class _StubChannel(Channel):
    """Controllable channel that records sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def listen(self, callback):
        pass

    def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))


# =============================================================================
# Acceptance Criteria: Core Scheduling
# =============================================================================


class TestAtJobFiresOnceAndAutoDeletes:
    """Create an `at` job in the near future → it fires once and auto-deletes."""

    def test_at_job_fires_via_scheduler_and_auto_deletes(self, tmp_path: Path):
        """Schedule an 'at' job 2s in the future; APScheduler fires it,
        executor runs, job is auto-deleted, history is preserved."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        future_ts = _future_iso(seconds=2)
        job = _make_job(
            "one-shot reminder",
            schedule=Schedule(kind="at", expr=future_ts),
        )
        mgr.add_job(job)
        mgr.start()

        _poll_until(lambda: executor.call_count >= 1, timeout=6)
        mgr.shutdown()

        # Executor should have been called exactly once
        assert executor.call_count == 1
        called_job = executor.call_args[0][0]
        assert called_job.id == job.id

        # Job should be auto-deleted from the store
        assert store.get(job.id) is None

        # But run history should be preserved
        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS

    def test_at_job_not_deleted_on_executor_failure(self, tmp_path: Path):
        """An 'at' job that fails should NOT be auto-deleted."""
        store = _make_store(tmp_path)
        executor = MagicMock(side_effect=RuntimeError("executor crashed"))
        mgr = CronManager(store, executor=executor)

        future_ts = _future_iso(seconds=2)
        job = _make_job(
            "failing reminder",
            schedule=Schedule(kind="at", expr=future_ts),
        )
        mgr.add_job(job)
        mgr.start()

        _poll_until(lambda: executor.call_count >= 1, timeout=6)
        mgr.shutdown()

        # Job should still exist (failure prevents auto-delete)
        assert store.get(job.id) is not None

        # Run history should record the failure
        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.FAILURE
        assert "executor crashed" in runs[0].error


class TestEveryJobFiresRepeatedly:
    """Create an `every` job with a short interval → it fires multiple times."""

    def test_every_job_fires_multiple_times(self, tmp_path: Path):
        """An 'every' job with 1s interval should fire at least twice in 3s."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "repeating",
            schedule=Schedule(kind="every", expr="1"),
        )
        mgr.add_job(job)
        mgr.start()

        _poll_until(lambda: executor.call_count >= 2, timeout=5)
        mgr.shutdown()

        # Should have fired at least twice
        assert executor.call_count >= 2

        # Each firing recorded in history
        runs = store.get_runs(job.id)
        assert len(runs) >= 2
        assert all(r.status == RunStatus.SUCCESS for r in runs)

    def test_every_job_not_auto_deleted(self, tmp_path: Path):
        """Interval jobs should persist after multiple firings."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "persistent interval",
            schedule=Schedule(kind="every", expr="1"),
        )
        mgr.add_job(job)
        mgr.start()

        _poll_until(lambda: executor.call_count >= 2, timeout=5)
        mgr.shutdown()

        # Job should still exist
        assert store.get(job.id) is not None


class TestPastOneShotFiresImmediately:
    """A one-shot job scheduled in the past should fire immediately."""

    def test_past_at_job_fires_immediately(self, tmp_path: Path):
        """An 'at' job with a past timestamp should fire as soon as the
        scheduler starts."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        past_ts = _past_iso(seconds=60)
        job = _make_job(
            "overdue reminder",
            schedule=Schedule(kind="at", expr=past_ts),
        )
        mgr.add_job(job)
        mgr.start()

        _poll_until(lambda: executor.call_count >= 1, timeout=5)
        mgr.shutdown()

        # Should have fired
        assert executor.call_count == 1

        # Should be auto-deleted on success
        assert store.get(job.id) is None

        # History preserved
        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS

    def test_past_at_job_retains_on_failure(self, tmp_path: Path):
        """A past one-shot that fails should retain the job for retry."""
        store = _make_store(tmp_path)
        executor = MagicMock(side_effect=RuntimeError("network error"))
        mgr = CronManager(store, executor=executor)

        past_ts = _past_iso(seconds=60)
        job = _make_job(
            "failed overdue",
            schedule=Schedule(kind="at", expr=past_ts),
        )
        mgr.add_job(job)
        mgr.start()

        _poll_until(lambda: executor.call_count >= 1, timeout=5)
        mgr.shutdown()

        # Should have attempted execution
        assert executor.call_count == 1

        # Job should still exist
        assert store.get(job.id) is not None


# =============================================================================
# Acceptance Criteria: End-to-End Flows
# =============================================================================


class TestEndToEndCLIFlow:
    """CLI add → scheduler fires → delivery routes → history recorded."""

    def test_add_trigger_deliver_history(self, tmp_path: Path):
        """Simulate the full CLI flow: add a job, trigger it, deliver output,
        and verify the run history is recorded."""
        store = _make_store(tmp_path)

        # Track delivery calls
        channel = _StubChannel()

        def channel_send(name: str, text: str) -> None:
            channel.send(name, text)

        # Executor that returns through delivery
        def executor_fn(job: CronJob) -> None:
            deliver(
                delivery=job.delivery,
                output="Morning briefing output",
                job=job,
                channel_send=channel_send,
            )

        mgr = CronManager(store, executor=executor_fn)

        # CLI: add a job with announce delivery
        job = _make_job(
            "morning briefing",
            schedule=Schedule(kind="cron", expr="0 8 * * *"),
            target="isolated",
            delivery=Delivery(mode="announce", channel="whatsapp"),
        )
        mgr.add_job(job)
        mgr.start()

        # CLI: trigger immediately
        mgr.trigger_job(job.id)

        mgr.shutdown()

        # Delivery should have routed to the channel
        assert len(channel.sent) == 1
        assert channel.sent[0] == ("whatsapp", "Morning briefing output")

        # Run history should be recorded
        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS

    def test_add_trigger_webhook_delivery(self, tmp_path: Path):
        """Job with webhook delivery POSTs output to the configured URL."""
        store = _make_store(tmp_path)

        posted: list[dict] = []

        def fake_post(url, json, timeout, **kwargs):
            posted.append({"url": url, "json": json})
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            return resp

        def executor_fn(job: CronJob) -> None:
            with patch("httpx.post", side_effect=fake_post):
                deliver(
                    delivery=job.delivery,
                    output="webhook output",
                    job=job,
                )

        mgr = CronManager(store, executor=executor_fn)

        job = _make_job(
            "webhook job",
            schedule=Schedule(kind="cron", expr="0 8 * * *"),
            target="isolated",
            delivery=Delivery(mode="webhook", url="https://example.com/hook"),
        )
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        assert len(posted) == 1
        assert posted[0]["url"] == "https://example.com/hook"
        assert posted[0]["json"]["output"] == "webhook output"
        assert posted[0]["json"]["job_name"] == "webhook job"

    def test_add_trigger_none_delivery(self, tmp_path: Path):
        """Job with none delivery runs silently."""
        store = _make_store(tmp_path)

        executed = []

        def executor_fn(job: CronJob) -> None:
            deliver(
                delivery=job.delivery,
                output="silent output",
                job=job,
            )
            executed.append(job.id)

        mgr = CronManager(store, executor=executor_fn)

        job = _make_job(
            "silent job",
            schedule=Schedule(kind="cron", expr="0 8 * * *"),
            target="isolated",
            delivery=Delivery(mode="none"),
        )
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        assert len(executed) == 1
        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS

    def test_best_effort_delivery_failure_still_succeeds(self, tmp_path: Path):
        """If delivery fails with best_effort=True, the job run still succeeds."""
        store = _make_store(tmp_path)

        def executor_fn(job: CronJob) -> None:
            # channel_send is None, but best_effort should swallow the error
            deliver(
                delivery=job.delivery,
                output="output",
                job=job,
                channel_send=None,  # Will cause RuntimeError
            )

        mgr = CronManager(store, executor=executor_fn)

        job = _make_job(
            "best-effort",
            schedule=Schedule(kind="cron", expr="0 8 * * *"),
            target="isolated",
            delivery=Delivery(mode="announce", channel="missing", best_effort=True),
        )
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        # The job should still succeed because best_effort swallows delivery errors
        runs = store.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS


class TestEndToEndAgentToolFlow:
    """Agent creates a job via the cron tool → it can be triggered → session injection."""

    def test_agent_creates_and_triggers_main_session_job(self, tmp_path: Path):
        """Agent adds a job via tool, CronManager triggers it, system event
        is injected into the chat session."""
        store = _make_store(tmp_path)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        chat_server = _StubChatServer(sessions_dir)

        def inject_event(text: str) -> None:
            chat_server.inject_system_event("main", text)

        executor = JobExecutor(inject_event=inject_event)
        mgr = CronManager(store, executor=executor)

        # Agent adds a job via the tool
        result_json = handle_cron_tool(
            {
                "action": "add",
                "name": "standup reminder",
                "schedule_kind": "cron",
                "schedule_expr": "0 9 * * 1-5",
                "message": "Time for standup!",
                "payload_kind": "systemEvent",
                "target": "main",
            },
            mgr,
        )
        result = json.loads(result_json)
        assert result["status"] == "created"
        job_id = result["job"]["id"]

        # Start manager and trigger
        mgr.start()
        mgr.trigger_job(job_id)
        mgr.shutdown()

        # System event should have been injected
        assert len(chat_server.injected_events) == 1
        sender, text = chat_server.injected_events[0]
        assert sender == "main"
        assert "[Scheduled: standup reminder]" in text
        assert "Time for standup!" in text

        # History recorded
        runs = mgr.get_runs(job_id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS

    def test_agent_creates_at_job_via_tool_then_fires(self, tmp_path: Path):
        """Agent creates a one-shot 'at' job via tool → fires → auto-deletes."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        future_ts = _future_iso(seconds=2)
        result_json = handle_cron_tool(
            {
                "action": "add",
                "name": "quick reminder",
                "schedule_kind": "at",
                "schedule_expr": future_ts,
                "message": "Check the logs",
            },
            mgr,
        )
        result = json.loads(result_json)
        assert result["status"] == "created"
        job_id = result["job"]["id"]

        mgr.start()
        _poll_until(lambda: executor.call_count >= 1, timeout=6)
        mgr.shutdown()

        # Should have fired
        assert executor.call_count == 1

        # Should be auto-deleted
        assert mgr.store.get(job_id) is None

        # Agent can still view run history for deleted one-shot jobs
        runs_json = handle_cron_tool(
            {"action": "runs", "job_id": job_id},
            mgr,
        )
        runs_result = json.loads(runs_json)
        assert runs_result["count"] >= 1
        assert runs_result["job_name"] == "(deleted)"

    def test_agent_lists_update_and_removes_job(self, tmp_path: Path):
        """Full CRUD lifecycle via the agent tool."""
        store = _make_store(tmp_path)
        mgr = CronManager(store)

        # Add
        result = json.loads(
            handle_cron_tool(
                {
                    "action": "add",
                    "name": "daily report",
                    "schedule_kind": "every",
                    "schedule_expr": "3600",
                    "message": "Generate daily report",
                },
                mgr,
            )
        )
        assert result["status"] == "created"
        job_id = result["job"]["id"]

        # List
        result = json.loads(handle_cron_tool({"action": "list"}, mgr))
        assert result["count"] == 1
        assert result["jobs"][0]["name"] == "daily report"

        # Update
        result = json.loads(
            handle_cron_tool(
                {"action": "update", "job_id": job_id, "name": "weekly report"},
                mgr,
            )
        )
        assert result["status"] == "updated"
        assert result["job"]["name"] == "weekly report"

        # Remove
        result = json.loads(
            handle_cron_tool(
                {"action": "remove", "job_id": job_id},
                mgr,
            )
        )
        assert result["status"] == "removed"

        # List should be empty
        result = json.loads(handle_cron_tool({"action": "list"}, mgr))
        assert result["count"] == 0


# =============================================================================
# Acceptance Criteria: Execution Modes
# =============================================================================


class TestExecutionModes:
    """Main session and isolated execution modes."""

    def test_main_session_job_injects_event(self, tmp_path: Path):
        """A main-session job should inject a system event with the formatted
        message containing the job name and payload."""
        store = _make_store(tmp_path)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        chat_server = _StubChatServer(sessions_dir)

        def inject_event(text: str) -> None:
            chat_server.inject_system_event("main", text)

        executor = JobExecutor(inject_event=inject_event)
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "team sync",
            target="main",
            payload=Payload(kind="systemEvent", message="Time for the team sync!"),
        )
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        assert len(chat_server.injected_events) == 1
        _, text = chat_server.injected_events[0]
        assert "[Scheduled: team sync]" in text
        assert "Time for the team sync!" in text

    @patch("taskrunner.cron.executor.run_agent_loop")
    def test_isolated_job_runs_fresh_agent_turn(
        self, mock_agent_loop, minimal_agent_def, tmp_path: Path
    ):
        """An isolated job should run a fresh agent loop."""
        from dataclasses import dataclass
        from dataclasses import field as dc_field

        @dataclass
        class FakeResult:
            text: str = "Agent analysis complete"
            turns_used: int = 1
            tool_calls_made: int = 0
            stop_reason: str = "end_turn"
            tool_history: list = dc_field(default_factory=list)

        mock_agent_loop.return_value = FakeResult()

        store = _make_store(tmp_path)
        executor = JobExecutor(agent_def=minimal_agent_def)
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "background analysis",
            target="isolated",
            payload=Payload(
                kind="agentTurn",
                message="Analyze overnight logs",
            ),
            delivery=Delivery(mode="none"),
        )
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        mock_agent_loop.assert_called_once()
        call_kwargs = mock_agent_loop.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        if messages is None:
            messages = call_kwargs[0][0]
        assert messages[0]["content"] == "Analyze overnight logs"

    @patch("taskrunner.cron.executor.run_agent_loop")
    def test_isolated_job_model_override(self, mock_agent_loop, minimal_agent_def, tmp_path: Path):
        """An isolated job with a model override should use the specified model."""
        from dataclasses import dataclass
        from dataclasses import field as dc_field

        @dataclass
        class FakeResult:
            text: str = "Done"
            turns_used: int = 1
            tool_calls_made: int = 0
            stop_reason: str = "end_turn"
            tool_history: list = dc_field(default_factory=list)

        mock_agent_loop.return_value = FakeResult()

        store = _make_store(tmp_path)
        executor = JobExecutor(agent_def=minimal_agent_def)
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "model test",
            target="isolated",
            payload=Payload(
                kind="agentTurn",
                message="test",
                model="claude-opus-4-20250514",
            ),
            delivery=Delivery(mode="none"),
        )
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        call_kwargs = mock_agent_loop.call_args
        llm_config = call_kwargs.kwargs.get("llm_config") or call_kwargs[1].get("llm_config")
        if llm_config is None:
            llm_config = call_kwargs[0][1]
        assert llm_config.model == "claude-opus-4-20250514"


# =============================================================================
# Acceptance Criteria: Run History
# =============================================================================


class TestRunHistoryAcceptance:
    """Run history recording and capping."""

    def test_run_records_all_fields(self, tmp_path: Path):
        """Each run should record job_id, start_time, end_time, status, error."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job("tracked")
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        runs = store.get_runs(job.id)
        assert len(runs) == 1
        run = runs[0]
        assert run.job_id == job.id
        assert run.started_at is not None
        assert run.ended_at is not None
        assert run.status == RunStatus.SUCCESS
        assert run.error is None

        # Timestamps should be valid ISO 8601
        datetime.fromisoformat(run.started_at)
        datetime.fromisoformat(run.ended_at)

    def test_failure_run_includes_error(self, tmp_path: Path):
        """A failed run should include the error message."""
        store = _make_store(tmp_path)
        executor = MagicMock(side_effect=ValueError("bad input"))
        mgr = CronManager(store, executor=executor)

        job = _make_job("failing")
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        runs = store.get_runs(job.id)
        assert runs[0].status == RunStatus.FAILURE
        assert "bad input" in runs[0].error

    def test_history_capped_at_max(self, tmp_path: Path):
        """Run history should be capped at max_runs_per_job."""
        store = JobStore(
            jobs_path=tmp_path / "cron" / "jobs.json",
            runs_path=tmp_path / "cron" / "runs.json",
            max_runs_per_job=5,
        )
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job("capped")
        mgr.add_job(job)
        mgr.start()

        for _ in range(10):
            mgr.trigger_job(job.id)

        mgr.shutdown()

        runs = store.get_runs(job.id)
        assert len(runs) == 5


# =============================================================================
# Acceptance Criteria: Enable / Disable
# =============================================================================


class TestEnableDisableAcceptance:
    """Disable a job → it stops firing; re-enable → it resumes."""

    def test_disable_stops_firing(self, tmp_path: Path):
        """A disabled interval job should not fire even though the scheduler
        is running."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "togglable",
            schedule=Schedule(kind="every", expr="1"),
        )
        mgr.add_job(job)
        mgr.start()

        # Let it fire once
        _poll_until(lambda: executor.call_count >= 1, timeout=5)
        first_count = executor.call_count
        assert first_count >= 1

        # Disable it
        mgr.disable_job(job.id)
        time.sleep(2)

        # Should not have fired more
        assert executor.call_count == first_count

        mgr.shutdown()

    def test_reenable_resumes_firing(self, tmp_path: Path):
        """Re-enabling a job should make it fire again."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        job = _make_job(
            "togglable",
            schedule=Schedule(kind="every", expr="1"),
            enabled=False,
        )
        mgr.add_job(job)
        mgr.start()

        # Should not fire when disabled
        time.sleep(1.5)
        assert executor.call_count == 0

        # Re-enable
        mgr.enable_job(job.id)
        _poll_until(lambda: executor.call_count >= 1, timeout=5)

        # Should fire after re-enable
        assert executor.call_count >= 1

        mgr.shutdown()


# =============================================================================
# Acceptance Criteria: Persistence
# =============================================================================


class TestPersistenceAcceptance:
    """Jobs survive daemon restarts."""

    def test_jobs_and_history_persist_across_restart(self, tmp_path: Path):
        """Jobs and their run history should survive store recreation."""
        cron_dir = tmp_path / "cron"

        # First lifecycle: add job and run it
        store1 = JobStore(
            jobs_path=cron_dir / "jobs.json",
            runs_path=cron_dir / "runs.json",
        )
        executor = MagicMock()
        mgr1 = CronManager(store1, executor=executor)

        job = _make_job("persistent")
        mgr1.add_job(job)
        mgr1.start()
        mgr1.trigger_job(job.id)
        mgr1.shutdown()

        # Second lifecycle: recreate store from same path
        store2 = JobStore(
            jobs_path=cron_dir / "jobs.json",
            runs_path=cron_dir / "runs.json",
        )
        mgr2 = CronManager(store2)

        # Job should be there
        reloaded = mgr2.get_job(job.id)
        assert reloaded is not None
        assert reloaded.name == "persistent"

        # History should be there
        runs = store2.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCasesAcceptance:
    """Edge case scenarios from the acceptance criteria."""

    def test_two_concurrent_jobs_both_run(self, tmp_path: Path):
        """Two jobs scheduled at the same interval should both fire."""
        store = _make_store(tmp_path)
        executor = MagicMock()
        mgr = CronManager(store, executor=executor)

        j1 = _make_job("alpha", schedule=Schedule(kind="every", expr="1"))
        j2 = _make_job("beta", schedule=Schedule(kind="every", expr="1"))
        mgr.add_job(j1)
        mgr.add_job(j2)
        mgr.start()

        _poll_until(
            lambda: len(store.get_runs(j1.id)) >= 1 and len(store.get_runs(j2.id)) >= 1, timeout=5
        )
        mgr.shutdown()

        # Both should have history entries
        j1_runs = store.get_runs(j1.id)
        j2_runs = store.get_runs(j2.id)
        assert len(j1_runs) >= 1
        assert len(j2_runs) >= 1

    def test_failed_payload_does_not_disable_job(self, tmp_path: Path):
        """A failed execution should NOT disable the job."""
        store = _make_store(tmp_path)
        executor = MagicMock(side_effect=RuntimeError("kaboom"))
        mgr = CronManager(store, executor=executor)

        job = _make_job("resilient")
        mgr.add_job(job)
        mgr.start()

        mgr.trigger_job(job.id)
        mgr.shutdown()

        # Job should still be enabled
        assert mgr.get_job(job.id).enabled is True

        # Failure should be recorded
        runs = store.get_runs(job.id)
        assert runs[0].status == RunStatus.FAILURE

    def test_corrupt_jobs_json_recovery(self, tmp_path: Path):
        """A corrupt jobs.json should be handled gracefully."""
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text("THIS IS NOT JSON")

        store = JobStore(
            jobs_path=cron_dir / "jobs.json",
            runs_path=cron_dir / "runs.json",
        )

        # Should start empty, not crash
        assert len(store.list()) == 0

        # Should still be functional
        job = _make_job("recovery")
        store.add(job)
        assert store.get(job.id) is not None

    def test_corrupt_runs_json_recovery(self, tmp_path: Path):
        """A corrupt runs.json should be handled gracefully."""
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "runs.json").write_text("{broken")

        store = JobStore(
            jobs_path=cron_dir / "jobs.json",
            runs_path=cron_dir / "runs.json",
        )

        # Should start with empty history
        assert store.get_runs("nonexistent") == []

        # Should still be functional for adding runs
        record = RunRecord(
            job_id="test",
            started_at=datetime.now(UTC).isoformat(),
            ended_at=datetime.now(UTC).isoformat(),
            status=RunStatus.SUCCESS,
        )
        store.add_run(record)
        assert len(store.get_runs("test")) == 1
