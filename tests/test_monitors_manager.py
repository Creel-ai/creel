"""Tests for MonitorManager — scheduling, deduplication, quiet hours, delivery."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from creel.cron.models import Delivery, Schedule
from creel.monitors.manager import MonitorManager
from creel.monitors.models import (
    AlertLevel,
    Monitor,
    MonitorRunStatus,
    QuietHours,
)
from creel.monitors.store import MonitorStore


def _make_store(tmp_path) -> MonitorStore:
    return MonitorStore(
        monitors_path=tmp_path / "monitors.json",
        runs_path=tmp_path / "runs.json",
        alerts_path=tmp_path / "alerts.json",
    )


def _make_monitor(name: str = "test", **kwargs) -> Monitor:
    defaults = dict(
        name=name,
        schedule=Schedule(kind="cron", expr="0 8 * * *"),
        executor="gmail_readonly",
        prompt="Check stuff",
    )
    defaults.update(kwargs)
    return Monitor(**defaults)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestMonitorManagerCRUD:
    def test_add_and_list(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store)
        mon = _make_monitor()
        mgr.add_monitor(mon)
        assert len(mgr.list_monitors()) == 1

    def test_get_monitor(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store)
        mon = _make_monitor()
        mgr.add_monitor(mon)
        assert mgr.get_monitor(mon.id) is not None

    def test_remove_monitor(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store)
        mon = _make_monitor()
        mgr.add_monitor(mon)
        mgr.remove_monitor(mon.id)
        assert mgr.get_monitor(mon.id) is None

    def test_enable_disable(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store)
        mon = _make_monitor(enabled=True)
        mgr.add_monitor(mon)

        updated = mgr.disable_monitor(mon.id)
        assert updated.enabled is False

        updated = mgr.enable_monitor(mon.id)
        assert updated.enabled is True

    def test_trigger_nonexistent_raises(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store)
        with pytest.raises(KeyError):
            mgr.trigger_monitor("nonexistent")

    def test_add_disallowed_executor_rejected(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store, allowed_executors={"gmail_readonly", "gcal"})
        mon = _make_monitor(executor="exec")
        with pytest.raises(ValueError, match="not in the allowed set"):
            mgr.add_monitor(mon)

    def test_add_allowed_executor_accepted(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store, allowed_executors={"gmail_readonly", "gcal"})
        mon = _make_monitor(executor="gmail_readonly")
        mgr.add_monitor(mon)
        assert mgr.get_monitor(mon.id) is not None

    def test_no_allowlist_allows_all(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store)
        mon = _make_monitor(executor="exec")
        mgr.add_monitor(mon)
        assert mgr.get_monitor(mon.id) is not None

    def test_update_disallowed_executor_rejected(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store, allowed_executors={"gmail_readonly", "gcal"})
        mon = _make_monitor(executor="gmail_readonly")
        mgr.add_monitor(mon)
        with pytest.raises(ValueError, match="not in the allowed set"):
            mgr.update_monitor(mon.id, executor="exec")


# ---------------------------------------------------------------------------
# Execution — OK (nothing to report)
# ---------------------------------------------------------------------------


class TestMonitorExecutionOK:
    def test_no_alert_records_ok(self, tmp_path) -> None:
        store = _make_store(tmp_path)

        def executor(mon: Monitor) -> str | None:
            return None

        mgr = MonitorManager(store=store, executor=executor)
        mon = _make_monitor()
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.OK
        assert record.alert_level is None

    def test_empty_string_is_ok(self, tmp_path) -> None:
        store = _make_store(tmp_path)

        def executor(mon: Monitor) -> str | None:
            return ""

        mgr = MonitorManager(store=store, executor=executor)
        mon = _make_monitor()
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.OK


# ---------------------------------------------------------------------------
# Execution — Alert generated and delivered
# ---------------------------------------------------------------------------


class TestMonitorExecutionAlert:
    def test_alert_delivered(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock()

        def executor(mon: Monitor) -> str | None:
            return "Disk 90% full!"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            delivery=Delivery(mode="announce", channel="telegram"),
            alert_level=AlertLevel.URGENT,
        )
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.ALERTED
        assert record.alert_level == AlertLevel.URGENT
        channel_send.assert_called_once()

        # Check alert was stored
        alerts = store.get_alerts(mon.id)
        assert len(alerts) == 1
        assert alerts[0].delivered is True

    def test_alert_with_delivery_none_still_records(self, tmp_path) -> None:
        store = _make_store(tmp_path)

        def executor(mon: Monitor) -> str | None:
            return "Something happened"

        mgr = MonitorManager(store=store, executor=executor)
        mon = _make_monitor(
            delivery=Delivery(mode="none"),
            alert_level=AlertLevel.NOTICE,
        )
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.ALERTED

        alerts = store.get_alerts(mon.id)
        assert len(alerts) == 1
        assert alerts[0].delivered is False


# ---------------------------------------------------------------------------
# Execution — Failure
# ---------------------------------------------------------------------------


class TestMonitorExecutionFailure:
    def test_executor_exception_records_failure(self, tmp_path) -> None:
        store = _make_store(tmp_path)

        def executor(mon: Monitor) -> str | None:
            raise RuntimeError("API down")

        mgr = MonitorManager(store=store, executor=executor)
        mon = _make_monitor()
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.FAILURE
        assert "API down" in (record.error or "")

    def test_no_executor_records_failure(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        mgr = MonitorManager(store=store, executor=None)
        mon = _make_monitor()
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.FAILURE


# ---------------------------------------------------------------------------
# Alert suppression — info level
# ---------------------------------------------------------------------------


class TestInfoLevelSuppression:
    def test_info_alert_suppressed(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock()

        def executor(mon: Monitor) -> str | None:
            return "Informational note"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            alert_level=AlertLevel.INFO,
            delivery=Delivery(mode="announce", channel="telegram"),
        )
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.SUPPRESSED
        assert record.suppressed_reason == "info_level"
        channel_send.assert_not_called()

        alerts = store.get_alerts(mon.id)
        assert len(alerts) == 1
        assert alerts[0].delivered is False


# ---------------------------------------------------------------------------
# Alert suppression — quiet hours
# ---------------------------------------------------------------------------


class TestQuietHoursSuppression:
    def test_notice_suppressed_during_quiet(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock()

        def executor(mon: Monitor) -> str | None:
            return "Something to report"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        # Set quiet hours to a window that includes "now"
        # Use a 24-hour window to guarantee we're in it
        mon = _make_monitor(
            alert_level=AlertLevel.NOTICE,
            quiet_hours=QuietHours(start="00:00", end="23:59", timezone="UTC"),
            delivery=Delivery(mode="announce", channel="telegram"),
        )
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.SUPPRESSED
        assert record.suppressed_reason == "quiet_hours"
        channel_send.assert_not_called()

    def test_urgent_bypasses_quiet_hours(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock()

        def executor(mon: Monitor) -> str | None:
            return "URGENT: Server down!"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            alert_level=AlertLevel.URGENT,
            quiet_hours=QuietHours(start="00:00", end="23:59", timezone="UTC"),
            delivery=Delivery(mode="announce", channel="telegram"),
        )
        mgr.add_monitor(mon)

        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.ALERTED
        channel_send.assert_called_once()


# ---------------------------------------------------------------------------
# Alert deduplication (cooldown)
# ---------------------------------------------------------------------------


class TestCooldownDedup:
    def test_same_alert_within_cooldown_suppressed(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock()

        def executor(mon: Monitor) -> str | None:
            return "Disk full!"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            alert_level=AlertLevel.NOTICE,
            cooldown_seconds=3600,
            delivery=Delivery(mode="announce", channel="telegram"),
        )
        mgr.add_monitor(mon)

        # First trigger — should deliver
        record1 = mgr.trigger_monitor(mon.id)
        assert record1.status == MonitorRunStatus.ALERTED
        assert channel_send.call_count == 1

        # Second trigger — same message, should be suppressed by cooldown
        record2 = mgr.trigger_monitor(mon.id)
        assert record2.status == MonitorRunStatus.SUPPRESSED
        assert record2.suppressed_reason == "cooldown"
        assert channel_send.call_count == 1  # not called again

    def test_different_alert_not_deduped(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock()

        alerts = iter(["Disk 85% full", "Memory low: 200MB free"])

        def executor(mon: Monitor) -> str | None:
            return next(alerts)

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            alert_level=AlertLevel.NOTICE,
            cooldown_seconds=3600,
            delivery=Delivery(mode="announce", channel="telegram"),
        )
        mgr.add_monitor(mon)

        record1 = mgr.trigger_monitor(mon.id)
        assert record1.status == MonitorRunStatus.ALERTED

        record2 = mgr.trigger_monitor(mon.id)
        assert record2.status == MonitorRunStatus.ALERTED

        assert channel_send.call_count == 2

    def test_zero_cooldown_no_dedup(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock()

        def executor(mon: Monitor) -> str | None:
            return "Same alert"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            alert_level=AlertLevel.NOTICE,
            cooldown_seconds=0,
            delivery=Delivery(mode="announce", channel="telegram"),
        )
        mgr.add_monitor(mon)

        mgr.trigger_monitor(mon.id)
        mgr.trigger_monitor(mon.id)
        assert channel_send.call_count == 2


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class TestMonitorDelivery:
    def test_announce_delivery(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock()

        def executor(mon: Monitor) -> str | None:
            return "Alert message"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            alert_level=AlertLevel.URGENT,
            delivery=Delivery(mode="announce", channel="telegram"),
        )
        mgr.add_monitor(mon)
        mgr.trigger_monitor(mon.id)

        channel_send.assert_called_once()
        call_args = channel_send.call_args
        assert call_args[0][0] == "telegram"
        assert "[URGENT]" in call_args[0][1]
        assert "Alert message" in call_args[0][1]

    def test_no_channel_send_logs_error(self, tmp_path) -> None:
        store = _make_store(tmp_path)

        def executor(mon: Monitor) -> str | None:
            return "Alert"

        mgr = MonitorManager(store=store, executor=executor, channel_send=None)
        mon = _make_monitor(
            alert_level=AlertLevel.URGENT,
            delivery=Delivery(mode="announce", channel="telegram"),
        )
        mgr.add_monitor(mon)

        # Should not raise, just log error — but alert marked as not delivered
        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.ALERTED

        alerts = store.get_alerts(mon.id)
        assert len(alerts) == 1
        assert alerts[0].delivered is False

    def test_delivery_failure_records_run(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock(side_effect=RuntimeError("connection refused"))

        def executor(mon: Monitor) -> str | None:
            return "Alert"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            alert_level=AlertLevel.URGENT,
            delivery=Delivery(mode="announce", channel="telegram", best_effort=False),
        )
        mgr.add_monitor(mon)

        # Non-best-effort delivery failure should record a FAILURE run, not crash
        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.FAILURE
        assert "Delivery failed: RuntimeError" == record.error

        # Alert should be recorded as not delivered with sanitized reason
        alerts = store.get_alerts(mon.id)
        assert len(alerts) == 1
        assert alerts[0].delivered is False
        assert alerts[0].suppressed_reason == "delivery_error: RuntimeError"

    def test_best_effort_delivery_failure_still_alerts(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        channel_send = MagicMock(side_effect=RuntimeError("timeout"))

        def executor(mon: Monitor) -> str | None:
            return "Alert"

        mgr = MonitorManager(store=store, executor=executor, channel_send=channel_send)
        mon = _make_monitor(
            alert_level=AlertLevel.URGENT,
            delivery=Delivery(mode="announce", channel="telegram", best_effort=True),
        )
        mgr.add_monitor(mon)

        # best_effort delivery failure should still record ALERTED with delivered=False
        record = mgr.trigger_monitor(mon.id)
        assert record.status == MonitorRunStatus.ALERTED

        alerts = store.get_alerts(mon.id)
        assert len(alerts) == 1
        assert alerts[0].delivered is False


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


class TestMonitorRunHistory:
    def test_runs_recorded(self, tmp_path) -> None:
        store = _make_store(tmp_path)

        def executor(mon: Monitor) -> str | None:
            return None

        mgr = MonitorManager(store=store, executor=executor)
        mon = _make_monitor()
        mgr.add_monitor(mon)

        mgr.trigger_monitor(mon.id)
        mgr.trigger_monitor(mon.id)

        runs = mgr.get_runs(mon.id)
        assert len(runs) == 2
        assert all(r.status == MonitorRunStatus.OK for r in runs)
