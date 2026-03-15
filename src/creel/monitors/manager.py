"""MonitorManager — wraps MonitorStore + APScheduler for proactive monitoring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from creel.cron.models import ChannelSendFn, Schedule
from creel.monitors.models import (
    AlertLevel,
    AlertRecord,
    Monitor,
    MonitorRunRecord,
    MonitorRunStatus,
    fingerprint_alert,
    now_iso,
)
from creel.monitors.store import MonitorStore

logger = logging.getLogger(__name__)

# Type for the monitor executor callback: receives a Monitor, returns the
# alert text (or empty string / None if nothing to report).
MonitorExecutorFn = Callable[[Monitor], str | None]


def _make_trigger(schedule: Schedule) -> CronTrigger | IntervalTrigger:
    """Convert a Schedule model into an APScheduler trigger."""
    if schedule.kind == "cron":
        return CronTrigger.from_crontab(schedule.expr, timezone=schedule.tz)
    elif schedule.kind == "every":
        return IntervalTrigger(seconds=int(schedule.expr))
    else:
        raise ValueError(
            f"Monitors only support 'cron' and 'every' schedules, got '{schedule.kind}'"
        )


class MonitorManager:
    """High-level manager for proactive monitors.

    Handles scheduling, alert deduplication, quiet hours, and delivery.
    """

    def __init__(
        self,
        store: MonitorStore,
        executor: MonitorExecutorFn | None = None,
        channel_send: ChannelSendFn | None = None,
        allowed_executors: set[str] | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._channel_send = channel_send
        self._allowed_executors = allowed_executors
        self._scheduler = BackgroundScheduler()

    @property
    def store(self) -> MonitorStore:
        return self._store

    @property
    def running(self) -> bool:
        return self._scheduler.running

    # -- Lifecycle --

    def start(self) -> None:
        """Load all enabled monitors into the scheduler and start."""
        monitors = self._store.list()
        for mon in monitors:
            if mon.enabled:
                try:
                    self._schedule_monitor(mon)
                except Exception:
                    logger.exception(
                        "Failed to schedule monitor '%s' (%s) on startup — skipping",
                        mon.name,
                        mon.id,
                    )

        self._scheduler.start()
        enabled_count = sum(1 for m in monitors if m.enabled)
        logger.info(
            "MonitorManager started with %d monitors (%d enabled)",
            len(monitors),
            enabled_count,
        )

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the scheduler gracefully."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("MonitorManager shut down")

    # -- CRUD --

    def _validate_executor(self, monitor: Monitor) -> None:
        """Reject monitors that reference executors not in the allowlist."""
        if self._allowed_executors is not None and monitor.executor not in self._allowed_executors:
            raise ValueError(
                f"Executor '{monitor.executor}' is not in the allowed set: "
                f"{sorted(self._allowed_executors)}"
            )

    def add_monitor(self, monitor: Monitor) -> Monitor:
        """Add a new monitor and schedule it if enabled."""
        self._validate_executor(monitor)
        self._store.add(monitor)
        if monitor.enabled and self._scheduler.running:
            try:
                self._schedule_monitor(monitor)
            except Exception:
                self._store.remove(monitor.id)
                raise
        return monitor

    def get_monitor(self, monitor_id: str) -> Monitor | None:
        return self._store.get(monitor_id)

    def list_monitors(self) -> list[Monitor]:
        return self._store.list()

    def update_monitor(self, monitor_id: str, **fields: Any) -> Monitor:
        """Update fields on a monitor and reschedule."""
        if "executor" in fields and self._allowed_executors is not None:
            if fields["executor"] not in self._allowed_executors:
                raise ValueError(
                    f"Executor '{fields['executor']}' is not in the allowed set: "
                    f"{sorted(self._allowed_executors)}"
                )
        updated = self._store.update(monitor_id, **fields)
        self._unschedule_monitor(monitor_id)
        if updated.enabled and self._scheduler.running:
            self._schedule_monitor(updated)
        return updated

    def remove_monitor(self, monitor_id: str) -> Monitor:
        self._unschedule_monitor(monitor_id)
        return self._store.remove(monitor_id)

    def enable_monitor(self, monitor_id: str) -> Monitor:
        return self.update_monitor(monitor_id, enabled=True)

    def disable_monitor(self, monitor_id: str) -> Monitor:
        return self.update_monitor(monitor_id, enabled=False)

    def trigger_monitor(self, monitor_id: str) -> MonitorRunRecord:
        """Run a monitor check immediately, regardless of schedule."""
        monitor = self._store.get(monitor_id)
        if monitor is None:
            raise KeyError(f"Monitor '{monitor_id}' not found")
        return self._execute_monitor(monitor)

    def get_runs(self, monitor_id: str) -> list[MonitorRunRecord]:
        return self._store.get_runs(monitor_id)

    def get_alerts(self, monitor_id: str) -> list[AlertRecord]:
        return self._store.get_alerts(monitor_id)

    # -- Internal scheduling --

    def _schedule_monitor(self, monitor: Monitor) -> None:
        trigger = _make_trigger(monitor.schedule)
        self._scheduler.add_job(
            self._on_monitor_fire,
            trigger,
            args=[monitor.id],
            id=monitor.id,
            name=monitor.name,
            replace_existing=True,
        )

    def _unschedule_monitor(self, monitor_id: str) -> None:
        from apscheduler.jobstores.base import JobLookupError

        try:
            self._scheduler.remove_job(monitor_id)
        except JobLookupError:
            pass

    def _on_monitor_fire(self, monitor_id: str) -> None:
        """Called by APScheduler when a monitor's trigger fires."""
        monitor = self._store.get(monitor_id)
        if monitor is None:
            logger.warning("Fired monitor '%s' not found — skipping", monitor_id)
            return
        if not monitor.enabled:
            logger.debug("Monitor '%s' is disabled — skipping", monitor_id)
            return
        self._execute_monitor(monitor)

    def _execute_monitor(self, monitor: Monitor) -> MonitorRunRecord:
        """Run a monitor check: execute, evaluate, deduplicate, deliver."""
        started_at = now_iso()

        # Step 1: Execute the monitor check
        alert_text: str | None = None
        try:
            if self._executor is not None:
                alert_text = self._executor(monitor)
            else:
                logger.error(
                    "Monitor '%s' (%s) fired but no executor configured",
                    monitor.name,
                    monitor.id,
                )
                record = MonitorRunRecord(
                    monitor_id=monitor.id,
                    started_at=started_at,
                    ended_at=now_iso(),
                    status=MonitorRunStatus.FAILURE,
                    error="No executor configured",
                )
                self._store.add_run(record)
                return record
        except Exception as exc:
            logger.exception("Monitor '%s' (%s) check failed", monitor.name, monitor.id)
            record = MonitorRunRecord(
                monitor_id=monitor.id,
                started_at=started_at,
                ended_at=now_iso(),
                status=MonitorRunStatus.FAILURE,
                error=str(exc),
            )
            self._store.add_run(record)
            return record

        # Step 2: If nothing to report, record OK
        if not alert_text:
            record = MonitorRunRecord(
                monitor_id=monitor.id,
                started_at=started_at,
                ended_at=now_iso(),
                status=MonitorRunStatus.OK,
            )
            self._store.add_run(record)
            return record

        # Step 3: Determine if alert should be delivered
        fp = fingerprint_alert(monitor.id, alert_text)
        suppressed_reason = self._should_suppress(monitor, fp)

        if suppressed_reason:
            # Record suppressed alert
            alert_record = AlertRecord(
                monitor_id=monitor.id,
                timestamp=now_iso(),
                level=monitor.alert_level,
                fingerprint=fp,
                message=alert_text,
                delivered=False,
                suppressed_reason=suppressed_reason,
            )
            self._store.add_alert(alert_record)

            record = MonitorRunRecord(
                monitor_id=monitor.id,
                started_at=started_at,
                ended_at=now_iso(),
                status=MonitorRunStatus.SUPPRESSED,
                alert_level=monitor.alert_level,
                alert_fingerprint=fp,
                suppressed_reason=suppressed_reason,
            )
            self._store.add_run(record)
            return record

        # Step 4: Deliver the alert
        try:
            delivered = self._deliver_alert(monitor, alert_text)
        except Exception as exc:
            logger.exception(
                "Alert delivery failed for monitor '%s' (%s)", monitor.name, monitor.id
            )
            exc_type = type(exc).__name__
            alert_record = AlertRecord(
                monitor_id=monitor.id,
                timestamp=now_iso(),
                level=monitor.alert_level,
                fingerprint=fp,
                message=alert_text,
                delivered=False,
                suppressed_reason=f"delivery_error: {exc_type}",
            )
            self._store.add_alert(alert_record)

            record = MonitorRunRecord(
                monitor_id=monitor.id,
                started_at=started_at,
                ended_at=now_iso(),
                status=MonitorRunStatus.FAILURE,
                alert_level=monitor.alert_level,
                alert_fingerprint=fp,
                error=f"Delivery failed: {exc_type}",
            )
            self._store.add_run(record)
            return record

        alert_record = AlertRecord(
            monitor_id=monitor.id,
            timestamp=now_iso(),
            level=monitor.alert_level,
            fingerprint=fp,
            message=alert_text,
            delivered=delivered,
        )
        self._store.add_alert(alert_record)

        record = MonitorRunRecord(
            monitor_id=monitor.id,
            started_at=started_at,
            ended_at=now_iso(),
            status=MonitorRunStatus.ALERTED,
            alert_level=monitor.alert_level,
            alert_fingerprint=fp,
        )
        self._store.add_run(record)
        return record

    def _should_suppress(self, monitor: Monitor, fingerprint: str) -> str | None:
        """Determine if an alert should be suppressed. Returns reason or None."""
        # Info-level alerts are always suppressed (logged only)
        if monitor.alert_level == AlertLevel.INFO:
            return "info_level"

        # Quiet hours check (urgent alerts bypass)
        if (
            monitor.alert_level != AlertLevel.URGENT
            and monitor.quiet_hours is not None
            and monitor.quiet_hours.is_quiet()
        ):
            return "quiet_hours"

        # Cooldown / dedup check
        if monitor.cooldown_seconds > 0:
            since = datetime.now(UTC) - timedelta(seconds=monitor.cooldown_seconds)
            recent_fps = self._store.get_recent_fingerprints(monitor.id, since)
            if fingerprint in recent_fps:
                return "cooldown"

        return None

    def _deliver_alert(self, monitor: Monitor, alert_text: str) -> bool:
        """Send alert via the configured delivery method.

        Returns True if the alert was actually sent, False otherwise.
        """
        delivery = monitor.delivery

        if delivery.mode == "none":
            logger.debug("Monitor '%s' delivery is 'none' — skipping send", monitor.name)
            return False

        prefix = f"[{monitor.alert_level.value.upper()}] {monitor.name}"
        full_message = f"{prefix}\n{alert_text}"

        if delivery.mode == "announce":
            if self._channel_send is None:
                logger.error(
                    "Cannot deliver alert for '%s': no channel_send callback", monitor.name
                )
                return False
            channel = delivery.channel
            if channel is None:
                logger.error("No channel configured for monitor '%s'", monitor.name)
                return False
            try:
                self._channel_send(channel, full_message)
                logger.info("Delivered alert for '%s' to channel '%s'", monitor.name, channel)
                return True
            except Exception:
                if delivery.best_effort:
                    logger.exception("Alert delivery failed for '%s' — best_effort", monitor.name)
                    return False
                else:
                    raise

        elif delivery.mode == "webhook":
            import httpx

            url = delivery.url
            if url is None:
                logger.error("No webhook URL for monitor '%s'", monitor.name)
                return False
            payload = {
                "monitor_id": monitor.id,
                "monitor_name": monitor.name,
                "alert_level": monitor.alert_level.value,
                "message": alert_text,
            }
            try:
                resp = httpx.post(url, json=payload, timeout=30, follow_redirects=False)
                resp.raise_for_status()
                logger.info("Delivered alert for '%s' to webhook %s", monitor.name, url)
                return True
            except Exception:
                if delivery.best_effort:
                    logger.exception("Webhook delivery failed for '%s'", monitor.name)
                    return False
                else:
                    raise

        return False
