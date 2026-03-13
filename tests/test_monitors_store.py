"""Tests for MonitorStore persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from creel.cron.models import Schedule
from creel.monitors.models import (
    AlertLevel,
    AlertRecord,
    Monitor,
    MonitorRunRecord,
    MonitorRunStatus,
)
from creel.monitors.store import MonitorStore


def _make_monitor(name: str = "test monitor", **kwargs) -> Monitor:
    defaults = dict(
        name=name,
        schedule=Schedule(kind="cron", expr="0 8 * * *"),
        executor="gmail_readonly",
        prompt="Check for stuff",
    )
    defaults.update(kwargs)
    return Monitor(**defaults)


def _make_run(monitor_id: str, status: MonitorRunStatus = MonitorRunStatus.OK) -> MonitorRunRecord:
    return MonitorRunRecord(
        monitor_id=monitor_id,
        started_at="2026-03-12T08:00:00+00:00",
        ended_at="2026-03-12T08:00:05+00:00",
        status=status,
    )


def _make_alert(
    monitor_id: str,
    fingerprint: str = "abc123",
    delivered: bool = True,
    timestamp: str = "2026-03-12T08:00:00+00:00",
) -> AlertRecord:
    return AlertRecord(
        monitor_id=monitor_id,
        timestamp=timestamp,
        level=AlertLevel.NOTICE,
        fingerprint=fingerprint,
        message="Something happened",
        delivered=delivered,
    )


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


class TestMonitorStoreBasics:
    def test_add_and_get(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)

        retrieved = store.get(mon.id)
        assert retrieved is not None
        assert retrieved.id == mon.id
        assert retrieved.name == "test monitor"

    def test_get_nonexistent(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        assert store.get("nonexistent") is None

    def test_add_duplicate_raises(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)
        with pytest.raises(ValueError, match="already exists"):
            store.add(mon)

    def test_list_empty(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        assert store.list() == []

    def test_list_returns_sorted_by_created_at(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        m1 = _make_monitor("first")
        m2 = _make_monitor("second")
        store.add(m1)
        store.add(m2)
        listed = store.list()
        assert len(listed) == 2
        assert listed[0].id == m1.id

    def test_update(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)
        updated = store.update(mon.id, name="new name", enabled=False)
        assert updated.name == "new name"
        assert updated.enabled is False

    def test_update_nonexistent_raises(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        with pytest.raises(KeyError):
            store.update("nonexistent", name="x")

    def test_update_id_raises(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)
        with pytest.raises(ValueError, match="Cannot change"):
            store.update(mon.id, id="newid")

    def test_remove(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)
        removed = store.remove(mon.id)
        assert removed.id == mon.id
        assert store.get(mon.id) is None

    def test_remove_nonexistent_raises(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        with pytest.raises(KeyError):
            store.remove("nonexistent")


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


class TestMonitorStoreRuns:
    def test_add_and_get_runs(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)

        run = _make_run(mon.id)
        store.add_run(run)

        runs = store.get_runs(mon.id)
        assert len(runs) == 1
        assert runs[0].status == MonitorRunStatus.OK

    def test_runs_capped(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
            max_runs_per_monitor=3,
        )
        mon = _make_monitor()
        store.add(mon)

        for _ in range(5):
            store.add_run(_make_run(mon.id))

        assert len(store.get_runs(mon.id)) == 3


# ---------------------------------------------------------------------------
# Alert records
# ---------------------------------------------------------------------------


class TestMonitorStoreAlerts:
    def test_add_and_get_alerts(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)

        alert = _make_alert(mon.id)
        store.add_alert(alert)

        alerts = store.get_alerts(mon.id)
        assert len(alerts) == 1
        assert alerts[0].fingerprint == "abc123"

    def test_alerts_capped(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
            max_alerts_per_monitor=3,
        )
        mon = _make_monitor()
        store.add(mon)

        for i in range(5):
            store.add_alert(_make_alert(mon.id, fingerprint=f"fp{i}"))

        assert len(store.get_alerts(mon.id)) == 3

    def test_get_recent_fingerprints(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)

        now = datetime.now(UTC)
        old_ts = (now - timedelta(hours=2)).isoformat()
        new_ts = (now - timedelta(minutes=30)).isoformat()

        store.add_alert(_make_alert(mon.id, fingerprint="old", timestamp=old_ts))
        store.add_alert(_make_alert(mon.id, fingerprint="new", timestamp=new_ts))

        since = now - timedelta(hours=1)
        fps = store.get_recent_fingerprints(mon.id, since)
        assert "new" in fps
        assert "old" not in fps

    def test_get_recent_fingerprints_excludes_undelivered(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)

        now = datetime.now(UTC)
        ts = (now - timedelta(minutes=10)).isoformat()
        store.add_alert(
            _make_alert(mon.id, fingerprint="suppressed", timestamp=ts, delivered=False)
        )

        since = now - timedelta(hours=1)
        fps = store.get_recent_fingerprints(mon.id, since)
        assert "suppressed" not in fps


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


class TestMonitorStorePersistence:
    def test_load_persisted_data(self, tmp_path) -> None:
        store1 = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store1.add(mon)
        store1.add_run(_make_run(mon.id))
        store1.add_alert(_make_alert(mon.id))

        # Create a new store from the same files
        store2 = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )

        assert store2.get(mon.id) is not None
        assert len(store2.get_runs(mon.id)) == 1
        assert len(store2.get_alerts(mon.id)) == 1

    def test_remove_cleans_history(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)
        store.add_run(_make_run(mon.id))
        store.add_alert(_make_alert(mon.id))

        store.remove(mon.id)
        assert store.get_runs(mon.id) == []
        assert store.get_alerts(mon.id) == []

    def test_remove_keep_history(self, tmp_path) -> None:
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        mon = _make_monitor()
        store.add(mon)
        store.add_run(_make_run(mon.id))

        store.remove(mon.id, keep_history=True)
        assert len(store.get_runs(mon.id)) == 1

    def test_corrupt_files_handled(self, tmp_path) -> None:
        # Write invalid JSON
        (tmp_path / "monitors.json").write_text("not json!")
        store = MonitorStore(
            monitors_path=tmp_path / "monitors.json",
            runs_path=tmp_path / "runs.json",
            alerts_path=tmp_path / "alerts.json",
        )
        assert store.list() == []
