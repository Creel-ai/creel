"""Data models for proactive monitor agents and alerts."""

from __future__ import annotations

import enum
import hashlib
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from creel.cron.models import Delivery, Schedule


class AlertLevel(enum.StrEnum):
    """Severity level for an alert."""

    INFO = "info"  # logged but not sent
    NOTICE = "notice"  # sent during active hours only
    URGENT = "urgent"  # sent immediately (ignores quiet hours)


class QuietHours(BaseModel):
    """Time window during which non-urgent alerts are suppressed."""

    start: str = "23:00"  # HH:MM in 24h format
    end: str = "07:00"
    timezone: str = "UTC"

    @field_validator("start", "end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        try:
            parts = v.split(":")
            if len(parts) != 2:
                raise ValueError
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except (ValueError, IndexError):
            raise ValueError(f"Time must be HH:MM in 24h format, got '{v}'") from None
        return v

    @field_validator("timezone")
    @classmethod
    def validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"Unknown timezone: '{v}'") from exc
        return v

    def is_quiet(self, now: datetime | None = None) -> bool:
        """Return True if the given time falls within quiet hours."""
        if now is None:
            now = datetime.now(UTC)
        tz = ZoneInfo(self.timezone)
        local = now.astimezone(tz)
        current_minutes = local.hour * 60 + local.minute

        start_parts = self.start.split(":")
        start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
        end_parts = self.end.split(":")
        end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])

        if start_minutes <= end_minutes:
            # Same-day range (e.g., 09:00-17:00)
            return start_minutes <= current_minutes < end_minutes
        else:
            # Overnight range (e.g., 23:00-07:00)
            return current_minutes >= start_minutes or current_minutes < end_minutes


class AlertRecord(BaseModel):
    """Record of a sent (or suppressed) alert."""

    monitor_id: str
    timestamp: str  # ISO 8601
    level: AlertLevel
    fingerprint: str
    message: str
    delivered: bool = True
    suppressed_reason: str | None = None  # "quiet_hours", "cooldown", "info_level"

    @field_validator("timestamp")
    @classmethod
    def validate_iso_timestamp(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError(f"timestamp must be ISO 8601, got '{v}'") from None
        return v


def _generate_id() -> str:
    """Generate a short, URL-safe monitor ID."""
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    """Current time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def fingerprint_alert(monitor_id: str, message: str) -> str:
    """Generate a fingerprint for alert deduplication.

    Uses monitor ID + first 200 chars of message to detect similar alerts.
    """
    content = f"{monitor_id}:{message[:200]}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class Monitor(BaseModel):
    """A proactive monitor agent definition."""

    id: str = Field(default_factory=_generate_id)
    name: str
    description: str = ""
    schedule: Schedule
    executor: str  # executor name (e.g., "gmail_readonly", "gcal", "exec")
    prompt: str  # what the monitor checks for
    delivery: Delivery = Field(default_factory=lambda: Delivery(mode="none"))
    alert_level: AlertLevel = AlertLevel.NOTICE
    quiet_hours: QuietHours | None = None
    cooldown_seconds: int = Field(default=3600, ge=0)  # 1 hour default
    enabled: bool = True
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class MonitorRunStatus(enum.StrEnum):
    """Outcome of a single monitor check."""

    OK = "ok"  # checked, nothing to report
    ALERTED = "alerted"  # alert was generated and delivered
    SUPPRESSED = "suppressed"  # alert generated but suppressed
    FAILURE = "failure"  # check failed


class MonitorRunRecord(BaseModel):
    """Record of a single monitor execution."""

    monitor_id: str
    started_at: str  # ISO 8601
    ended_at: str | None = None
    status: MonitorRunStatus
    alert_level: AlertLevel | None = None
    alert_fingerprint: str | None = None
    suppressed_reason: str | None = None
    error: str | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def validate_iso_timestamp(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                datetime.fromisoformat(v)
            except ValueError:
                raise ValueError(f"timestamp must be ISO 8601, got '{v}'") from None
        return v
