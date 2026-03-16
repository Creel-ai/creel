"""Tests for monitor data models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from creel.cron.models import Delivery, Schedule
from creel.monitors.models import (
    AlertLevel,
    AlertRecord,
    Monitor,
    MonitorRunRecord,
    MonitorRunStatus,
    QuietHours,
    fingerprint_alert,
    now_iso,
)

# ---------------------------------------------------------------------------
# AlertLevel
# ---------------------------------------------------------------------------


class TestAlertLevel:
    def test_values(self) -> None:
        assert AlertLevel.INFO == "info"
        assert AlertLevel.NOTICE == "notice"
        assert AlertLevel.URGENT == "urgent"


# ---------------------------------------------------------------------------
# QuietHours
# ---------------------------------------------------------------------------


class TestQuietHours:
    def test_valid_quiet_hours(self) -> None:
        qh = QuietHours(start="23:00", end="07:00", timezone="UTC")
        assert qh.start == "23:00"
        assert qh.end == "07:00"

    def test_invalid_time_format(self) -> None:
        with pytest.raises(ValueError, match="HH:MM"):
            QuietHours(start="25:00", end="07:00")

    def test_invalid_timezone(self) -> None:
        with pytest.raises(ValueError, match="Unknown timezone"):
            QuietHours(start="23:00", end="07:00", timezone="Not/A/Timezone")

    def test_is_quiet_overnight_during_quiet(self) -> None:
        qh = QuietHours(start="23:00", end="07:00", timezone="UTC")
        # 1am UTC should be quiet
        dt = datetime(2026, 3, 12, 1, 0, tzinfo=UTC)
        assert qh.is_quiet(dt) is True

    def test_is_quiet_overnight_before_quiet(self) -> None:
        qh = QuietHours(start="23:00", end="07:00", timezone="UTC")
        # 10am UTC should not be quiet
        dt = datetime(2026, 3, 12, 10, 0, tzinfo=UTC)
        assert qh.is_quiet(dt) is False

    def test_is_quiet_overnight_at_start(self) -> None:
        qh = QuietHours(start="23:00", end="07:00", timezone="UTC")
        dt = datetime(2026, 3, 12, 23, 0, tzinfo=UTC)
        assert qh.is_quiet(dt) is True

    def test_is_quiet_overnight_at_end(self) -> None:
        qh = QuietHours(start="23:00", end="07:00", timezone="UTC")
        # Exactly at end should NOT be quiet (end is exclusive)
        dt = datetime(2026, 3, 12, 7, 0, tzinfo=UTC)
        assert qh.is_quiet(dt) is False

    def test_is_quiet_same_day_range(self) -> None:
        qh = QuietHours(start="09:00", end="17:00", timezone="UTC")
        # 12pm UTC should be quiet
        dt = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
        assert qh.is_quiet(dt) is True

    def test_is_quiet_same_day_range_outside(self) -> None:
        qh = QuietHours(start="09:00", end="17:00", timezone="UTC")
        dt = datetime(2026, 3, 12, 20, 0, tzinfo=UTC)
        assert qh.is_quiet(dt) is False

    def test_is_quiet_with_timezone(self) -> None:
        # 11pm Denver = 5am UTC (next day) during standard time
        qh = QuietHours(start="23:00", end="07:00", timezone="America/Denver")
        # 6am UTC = midnight Denver (in quiet hours)
        dt = datetime(2026, 3, 12, 6, 0, tzinfo=UTC)
        assert qh.is_quiet(dt) is True


# ---------------------------------------------------------------------------
# fingerprint_alert
# ---------------------------------------------------------------------------


class TestFingerprintAlert:
    def test_same_input_same_fingerprint(self) -> None:
        fp1 = fingerprint_alert("mon1", "Server disk full")
        fp2 = fingerprint_alert("mon1", "Server disk full")
        assert fp1 == fp2

    def test_different_monitor_different_fingerprint(self) -> None:
        fp1 = fingerprint_alert("mon1", "Server disk full")
        fp2 = fingerprint_alert("mon2", "Server disk full")
        assert fp1 != fp2

    def test_different_message_different_fingerprint(self) -> None:
        fp1 = fingerprint_alert("mon1", "Disk 85% full")
        fp2 = fingerprint_alert("mon1", "Disk 90% full")
        assert fp1 != fp2

    def test_fingerprint_is_hex_string(self) -> None:
        fp = fingerprint_alert("mon1", "test")
        assert len(fp) == 16
        int(fp, 16)  # should not raise

    def test_long_message_truncated(self) -> None:
        msg = "x" * 500
        fp1 = fingerprint_alert("mon1", msg)
        fp2 = fingerprint_alert("mon1", msg[:200] + "y" * 300)
        # First 200 chars are the same, so fingerprints should match
        assert fp1 == fp2


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class TestMonitor:
    def test_create_basic_monitor(self) -> None:
        mon = Monitor(
            name="test",
            schedule=Schedule(kind="cron", expr="0 8 * * *"),
            executor="gmail_readonly",
            prompt="Check email",
        )
        assert mon.name == "test"
        assert mon.executor == "gmail_readonly"
        assert mon.enabled is True
        assert mon.alert_level == AlertLevel.NOTICE
        assert mon.cooldown_seconds == 3600
        assert len(mon.id) == 12

    def test_create_with_all_fields(self) -> None:
        mon = Monitor(
            name="full",
            description="A full monitor",
            schedule=Schedule(kind="every", expr="300"),
            executor="exec",
            prompt="Check things",
            delivery=Delivery(mode="announce", channel="telegram"),
            alert_level=AlertLevel.URGENT,
            quiet_hours=QuietHours(start="22:00", end="06:00"),
            cooldown_seconds=1800,
            enabled=False,
        )
        assert mon.alert_level == AlertLevel.URGENT
        assert mon.quiet_hours is not None
        assert mon.quiet_hours.start == "22:00"
        assert mon.enabled is False

    def test_delivery_none_ok_for_info(self) -> None:
        mon = Monitor(
            name="info-only",
            schedule=Schedule(kind="cron", expr="0 * * * *"),
            executor="exec",
            prompt="check",
            alert_level=AlertLevel.INFO,
            delivery=Delivery(mode="none"),
        )
        assert mon.delivery.mode == "none"


# ---------------------------------------------------------------------------
# MonitorRunRecord
# ---------------------------------------------------------------------------


class TestMonitorRunRecord:
    def test_create_ok_record(self) -> None:
        record = MonitorRunRecord(
            monitor_id="abc123",
            started_at="2026-03-12T08:00:00+00:00",
            ended_at="2026-03-12T08:00:05+00:00",
            status=MonitorRunStatus.OK,
        )
        assert record.status == MonitorRunStatus.OK
        assert record.alert_level is None

    def test_create_alerted_record(self) -> None:
        record = MonitorRunRecord(
            monitor_id="abc123",
            started_at="2026-03-12T08:00:00+00:00",
            ended_at="2026-03-12T08:00:05+00:00",
            status=MonitorRunStatus.ALERTED,
            alert_level=AlertLevel.URGENT,
            alert_fingerprint="abcd1234",
        )
        assert record.status == MonitorRunStatus.ALERTED
        assert record.alert_level == AlertLevel.URGENT

    def test_invalid_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="ISO 8601"):
            MonitorRunRecord(
                monitor_id="abc",
                started_at="not-a-date",
                status=MonitorRunStatus.OK,
            )


# ---------------------------------------------------------------------------
# AlertRecord
# ---------------------------------------------------------------------------


class TestAlertRecord:
    def test_create_delivered_alert(self) -> None:
        a = AlertRecord(
            monitor_id="mon1",
            timestamp="2026-03-12T08:00:00+00:00",
            level=AlertLevel.URGENT,
            fingerprint="abc123",
            message="Disk full!",
            delivered=True,
        )
        assert a.delivered is True
        assert a.suppressed_reason is None

    def test_create_suppressed_alert(self) -> None:
        a = AlertRecord(
            monitor_id="mon1",
            timestamp="2026-03-12T08:00:00+00:00",
            level=AlertLevel.NOTICE,
            fingerprint="abc123",
            message="Something happened",
            delivered=False,
            suppressed_reason="quiet_hours",
        )
        assert a.delivered is False
        assert a.suppressed_reason == "quiet_hours"


# ---------------------------------------------------------------------------
# now_iso
# ---------------------------------------------------------------------------


class TestNowIso:
    def test_returns_iso_string(self) -> None:
        result = now_iso()
        # Should be parseable
        datetime.fromisoformat(result)
