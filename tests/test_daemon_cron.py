"""Tests for Phase 6 — Daemon / CronManager integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from taskrunner.channels.base import Channel
from taskrunner.cron.models import CronJob, Delivery, Payload, Schedule
from taskrunner.cron.store import JobStore
from taskrunner.daemon.service import DaemonService
from taskrunner.session import SessionManager

# -- Stubs --


class _StubChatServer:
    """Minimal chat-server shape reused from test_daemon_service."""

    def __init__(self, sessions_dir: Path) -> None:
        self._session_mgr = SessionManager(sessions_dir=str(sessions_dir), max_history=50)
        self._guardian = None
        self.calls: list[tuple[str, str]] = []
        self.injected_events: list[tuple[str, str]] = []

    def handle_message(
        self,
        sender_id: str,
        text: str,
        on_text_delta=None,
        *,
        auto_approve: bool = False,
    ) -> str:
        self.calls.append((sender_id, text))
        session = self._session_mgr.add_user_message(sender_id, text)
        response = f"echo:{text}"
        session.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": response}]}
        )
        self._session_mgr.save_session(session)
        return response

    def inject_system_event(self, sender_id: str, text: str) -> None:
        self.injected_events.append((sender_id, text))
        self._session_mgr.add_user_message(sender_id, text)


class _StubChannel(Channel):
    """Controllable channel that records sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def listen(self, callback):
        pass

    def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))


# -- Fixtures --


@pytest.fixture
def cron_store(tmp_path: Path) -> JobStore:
    return JobStore(
        jobs_path=tmp_path / "cron" / "jobs.json",
        runs_path=tmp_path / "cron" / "runs.json",
    )


@pytest.fixture
def daemon_service(minimal_agent_def, tmp_path: Path, cron_store: JobStore) -> DaemonService:
    server = _StubChatServer(tmp_path / "sessions")
    return DaemonService(
        minimal_agent_def,
        server=server,
        cron_store=cron_store,
    )


def _make_job(
    name: str = "test job",
    target: str = "isolated",
    delivery_mode: str = "none",
    channel: str | None = None,
    schedule_kind: str = "cron",
    schedule_expr: str = "0 8 * * *",
) -> CronJob:
    delivery_kwargs: dict = {"mode": delivery_mode}
    if channel:
        delivery_kwargs["channel"] = channel
    return CronJob(
        name=name,
        schedule=Schedule(kind=schedule_kind, expr=schedule_expr),
        target=target,
        payload=Payload(message="do the thing"),
        delivery=Delivery(**delivery_kwargs),
    )


# -- CronManager initialization --


class TestCronManagerInit:
    def test_daemon_has_cron_manager(self, daemon_service: DaemonService) -> None:
        """DaemonService should expose a cron_manager property."""
        mgr = daemon_service.cron_manager
        assert mgr is not None
        assert not mgr.running

    def test_cron_manager_uses_provided_store(
        self, minimal_agent_def, tmp_path: Path, cron_store: JobStore
    ) -> None:
        """CronManager should use the store passed to DaemonService."""
        server = _StubChatServer(tmp_path / "sessions")
        svc = DaemonService(minimal_agent_def, server=server, cron_store=cron_store)

        assert svc.cron_manager.store is cron_store

    def test_cron_manager_default_store(self, minimal_agent_def, tmp_path: Path) -> None:
        """If no store is passed, DaemonService creates a default JobStore."""
        server = _StubChatServer(tmp_path / "sessions")
        svc = DaemonService(minimal_agent_def, server=server)

        assert svc.cron_manager.store is not None


# -- Start / stop lifecycle --


class TestCronManagerLifecycle:
    def test_start_cron_manager(self, daemon_service: DaemonService) -> None:
        """start_cron_manager should start the scheduler."""
        result = daemon_service.start_cron_manager()
        assert result is True
        assert daemon_service.cron_manager.running

        daemon_service.stop_cron_manager()

    def test_start_cron_manager_already_running(self, daemon_service: DaemonService) -> None:
        """start_cron_manager returns False if already running."""
        daemon_service.start_cron_manager()
        result = daemon_service.start_cron_manager()
        assert result is False

        daemon_service.stop_cron_manager()

    def test_stop_cron_manager(self, daemon_service: DaemonService) -> None:
        """stop_cron_manager should stop the scheduler."""
        daemon_service.start_cron_manager()
        result = daemon_service.stop_cron_manager()
        assert result is True
        assert not daemon_service.cron_manager.running

    def test_stop_cron_manager_not_running(self, daemon_service: DaemonService) -> None:
        """stop_cron_manager returns False when not running."""
        result = daemon_service.stop_cron_manager()
        assert result is False


# -- Event injection (main-session jobs) --


class TestEventInjection:
    def test_inject_cron_event_calls_chat_server(self, daemon_service: DaemonService) -> None:
        """_inject_cron_event should call inject_system_event on the chat server."""
        daemon_service._inject_cron_event("[Scheduled: test]\nHello!")

        server = daemon_service._server
        assert len(server.injected_events) == 1
        assert server.injected_events[0] == ("main", "[Scheduled: test]\nHello!")

    def test_inject_cron_event_custom_sender_id(
        self, minimal_agent_def, tmp_path: Path, cron_store: JobStore
    ) -> None:
        """Custom cron_sender_id should be used for event injection."""
        server = _StubChatServer(tmp_path / "sessions")
        svc = DaemonService(
            minimal_agent_def,
            server=server,
            cron_store=cron_store,
            cron_sender_id="scheduler",
        )

        svc._inject_cron_event("Hello from scheduler")
        assert server.injected_events[0][0] == "scheduler"

    def test_inject_event_adds_to_session_history(self, daemon_service: DaemonService) -> None:
        """Injected events should be visible in the session message history."""
        daemon_service._inject_cron_event("[Scheduled: test]\nReminder!")

        history = daemon_service.get_history("main", limit=10)
        assert len(history) == 1
        assert history[0]["role"] == "user"
        assert "Reminder!" in history[0]["content"]

    def test_main_session_job_triggers_injection(self, daemon_service: DaemonService) -> None:
        """A main-session cron job should inject its event into the chat server."""
        job = _make_job(name="reminder", target="main")
        daemon_service.cron_manager.store.add(job)

        daemon_service.start_cron_manager()

        # Trigger the job manually
        daemon_service.cron_manager.trigger_job(job.id)

        server = daemon_service._server
        assert len(server.injected_events) == 1
        sender_id, text = server.injected_events[0]
        assert sender_id == "main"
        assert "[Scheduled: reminder]" in text
        assert "do the thing" in text

        daemon_service.stop_cron_manager()


# -- Channel delivery --


class TestChannelDelivery:
    def test_channel_send_routes_to_channel(self, daemon_service: DaemonService) -> None:
        """_channel_send should forward to the registered channel."""
        channel = _StubChannel()
        daemon_service.register_channel("whatsapp", channel)

        daemon_service._channel_send("whatsapp", "Hello from cron!")

        assert len(channel.sent) == 1
        # Recipient should be the cron_sender_id ("main"), not the channel name
        assert channel.sent[0] == ("main", "Hello from cron!")

    def test_channel_send_unknown_channel_raises(self, daemon_service: DaemonService) -> None:
        """_channel_send should raise ValueError for unknown channels."""
        with pytest.raises(ValueError, match="not found for cron delivery"):
            daemon_service._channel_send("nonexistent", "Hello!")

    @patch("taskrunner.cron.executor.run_agent_loop")
    def test_isolated_job_deliver_to_channel(
        self, mock_agent_loop, daemon_service: DaemonService, tmp_path: Path
    ) -> None:
        """An isolated job with announce delivery should route through the channel."""
        from dataclasses import dataclass
        from dataclasses import field as dc_field

        @dataclass
        class FakeResult:
            text: str = "Agent output"
            turns_used: int = 1
            tool_calls_made: int = 0
            stop_reason: str = "end_turn"
            tool_history: list = dc_field(default_factory=list)

        mock_agent_loop.return_value = FakeResult()

        channel = _StubChannel()
        daemon_service.register_channel("whatsapp", channel)

        job = _make_job(
            name="briefing",
            target="isolated",
            delivery_mode="announce",
            channel="whatsapp",
        )
        daemon_service.cron_manager.store.add(job)

        daemon_service.start_cron_manager()

        daemon_service.cron_manager.trigger_job(job.id)

        assert len(channel.sent) == 1
        assert channel.sent[0][1] == "Agent output"

        daemon_service.stop_cron_manager()


# -- Graceful shutdown --


class TestGracefulShutdown:
    def test_shutdown_stops_cron_manager(self, daemon_service: DaemonService) -> None:
        """shutdown() should stop the cron manager."""
        daemon_service.start_cron_manager()
        assert daemon_service.cron_manager.running

        daemon_service.shutdown()
        assert not daemon_service.cron_manager.running

    def test_shutdown_idempotent(self, daemon_service: DaemonService) -> None:
        """Calling shutdown() twice should not error."""
        daemon_service.shutdown()
        daemon_service.shutdown()  # no-op

    def test_shutdown_without_cron_running(self, daemon_service: DaemonService) -> None:
        """shutdown() should work fine when cron manager was never started."""
        daemon_service.shutdown()
        assert not daemon_service.cron_manager.running


# -- Status reporting --


class TestCronStatus:
    def test_status_includes_cron_section(self, daemon_service: DaemonService) -> None:
        """status() should include a 'cron' section."""
        status = daemon_service.status()
        assert "cron" in status
        assert status["cron"]["running"] is False
        assert status["cron"]["managed_jobs"] == 0

    def test_status_counts_managed_jobs(self, daemon_service: DaemonService) -> None:
        """Status should reflect the number of managed jobs."""
        job = _make_job(name="job1")
        daemon_service.cron_manager.store.add(job)

        status = daemon_service.status()
        assert status["cron"]["managed_jobs"] == 1

    def test_status_cron_running_after_start(self, daemon_service: DaemonService) -> None:
        """Status should show cron as running after start_cron_manager."""
        daemon_service.start_cron_manager()
        status = daemon_service.status()
        assert status["cron"]["running"] is True

        daemon_service.stop_cron_manager()
        status = daemon_service.status()
        assert status["cron"]["running"] is False


# -- Jobs persist across restart --


class TestJobPersistence:
    def test_jobs_survive_restart(self, minimal_agent_def, tmp_path: Path) -> None:
        """Jobs added via cron_manager should survive a DaemonService restart."""
        cron_dir = tmp_path / "cron"
        store1 = JobStore(
            jobs_path=cron_dir / "jobs.json",
            runs_path=cron_dir / "runs.json",
        )

        server1 = _StubChatServer(tmp_path / "sessions1")
        svc1 = DaemonService(minimal_agent_def, server=server1, cron_store=store1)

        job = _make_job(name="persistent-job")
        svc1.cron_manager.add_job(job)
        assert len(svc1.cron_manager.store.list()) == 1

        # Simulate restart: create a new store from the same path
        store2 = JobStore(
            jobs_path=cron_dir / "jobs.json",
            runs_path=cron_dir / "runs.json",
        )
        server2 = _StubChatServer(tmp_path / "sessions2")
        svc2 = DaemonService(minimal_agent_def, server=server2, cron_store=store2)

        # The job should still be there
        jobs = svc2.cron_manager.store.list()
        assert len(jobs) == 1
        assert jobs[0].name == "persistent-job"


# -- Run history recording --


class TestRunHistoryRecording:
    def test_trigger_records_run(self, daemon_service: DaemonService, tmp_path: Path) -> None:
        """Triggering a main-session job should record a run in history."""
        job = _make_job(name="tracked-job", target="main")
        daemon_service.cron_manager.store.add(job)

        daemon_service.start_cron_manager()

        daemon_service.cron_manager.trigger_job(job.id)

        runs = daemon_service.cron_manager.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status.value == "success"
        assert runs[0].job_id == job.id

        daemon_service.stop_cron_manager()

    @patch("taskrunner.cron.executor.run_agent_loop")
    def test_failed_job_records_failure(
        self, mock_agent_loop, daemon_service: DaemonService, tmp_path: Path
    ) -> None:
        """A failing isolated job should record a failure run and stay enabled."""
        mock_agent_loop.side_effect = RuntimeError("LLM unavailable")

        job = _make_job(name="flaky-job", target="isolated")
        daemon_service.cron_manager.store.add(job)

        daemon_service.start_cron_manager()

        daemon_service.cron_manager.trigger_job(job.id)

        runs = daemon_service.cron_manager.get_runs(job.id)
        assert len(runs) == 1
        assert runs[0].status.value == "failure"
        assert "LLM unavailable" in runs[0].error

        # Job should remain enabled for next run
        reloaded = daemon_service.cron_manager.get_job(job.id)
        assert reloaded.enabled is True

        daemon_service.stop_cron_manager()


# -- Edge cases --


class TestEdgeCases:
    def test_corrupt_jobs_json_starts_empty(self, minimal_agent_def, tmp_path: Path) -> None:
        """A corrupt jobs.json should not prevent startup."""
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text("NOT VALID JSON!!!")

        store = JobStore(
            jobs_path=cron_dir / "jobs.json",
            runs_path=cron_dir / "runs.json",
        )

        server = _StubChatServer(tmp_path / "sessions")
        svc = DaemonService(minimal_agent_def, server=server, cron_store=store)

        assert len(svc.cron_manager.store.list()) == 0

        result = svc.start_cron_manager()
        assert result is True

        svc.stop_cron_manager()

    def test_two_jobs_both_run_on_trigger(
        self, daemon_service: DaemonService, tmp_path: Path
    ) -> None:
        """Two jobs triggered concurrently should both execute."""
        job1 = _make_job(name="job-a", target="main")
        job2 = _make_job(name="job-b", target="main")
        daemon_service.cron_manager.store.add(job1)
        daemon_service.cron_manager.store.add(job2)

        daemon_service.start_cron_manager()

        daemon_service.cron_manager.trigger_job(job1.id)
        daemon_service.cron_manager.trigger_job(job2.id)

        server = daemon_service._server
        assert len(server.injected_events) == 2

        names = {text.split("\n")[0] for _, text in server.injected_events}
        assert "[Scheduled: job-a]" in names
        assert "[Scheduled: job-b]" in names

        daemon_service.stop_cron_manager()
