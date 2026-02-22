"""Data models for the dynamic cron / scheduled jobs system."""

from __future__ import annotations

import enum
import ipaddress
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

# Shared callback type for channel delivery.
# Receives (channel_name, message_text).
ChannelSendFn = Callable[[str, str], None]


class Schedule(BaseModel):
    """When a job should run.

    Three kinds:
      - cron: standard 5-part cron expression
      - every: fixed interval in seconds
      - at: one-shot ISO 8601 timestamp
    """

    kind: Literal["cron", "every", "at"]
    expr: str
    tz: str = "UTC"

    @field_validator("tz")
    @classmethod
    def validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (KeyError, Exception):
            raise ValueError(
                f"Unknown timezone: '{v}'. "
                "Use IANA timezone names like 'America/Denver' or 'UTC'."
            )
        return v

    @field_validator("expr")
    @classmethod
    def validate_expr(cls, v: str, info) -> str:
        kind = info.data.get("kind")
        if kind == "cron":
            parts = v.split()
            if len(parts) != 5:
                raise ValueError(
                    f"cron expression must have 5 parts, got {len(parts)}"
                )
            try:
                from apscheduler.triggers.cron import CronTrigger

                CronTrigger.from_crontab(v)
            except ValueError as e:
                raise ValueError(f"Invalid cron expression '{v}': {e}")
        elif kind == "every":
            try:
                seconds = int(v)
            except ValueError:
                raise ValueError(
                    f"'every' schedule expr must be an integer (seconds), got '{v}'"
                )
            if seconds < 1:
                raise ValueError(
                    f"'every' interval must be >= 1 second, got {seconds}"
                )
        elif kind == "at":
            try:
                datetime.fromisoformat(v)
            except ValueError:
                raise ValueError(
                    f"'at' schedule expr must be ISO 8601, got '{v}'"
                )
        return v


class Payload(BaseModel):
    """What to do when a job fires.

    Two kinds:
      - agentTurn: run a full agent loop with the given message
      - systemEvent: inject a system event into the main session
    """

    kind: Literal["agentTurn", "systemEvent"] = "agentTurn"
    message: str
    model: str | None = None
    timeout_seconds: int = 120


class Delivery(BaseModel):
    """How to deliver output from isolated jobs.

    Modes:
      - announce: send output to a chat channel
      - webhook: POST output to a URL
      - none: run silently
    """

    mode: Literal["announce", "webhook", "none"] = "announce"
    channel: str | None = None
    url: str | None = None
    best_effort: bool = True

    @model_validator(mode="after")
    def check_required_fields(self) -> Delivery:
        if self.mode == "announce" and not self.channel:
            raise ValueError("channel is required when delivery mode is 'announce'")
        if self.mode == "webhook":
            if not self.url:
                raise ValueError("url is required when delivery mode is 'webhook'")
            _validate_webhook_url(self.url)
        return self


class RunStatus(str, enum.Enum):
    """Outcome of a single job run."""

    SUCCESS = "success"
    FAILURE = "failure"


class RunRecord(BaseModel):
    """Record of a single job execution."""

    job_id: str
    started_at: str  # ISO 8601
    ended_at: str | None = None  # ISO 8601
    status: RunStatus
    error: str | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def validate_iso_timestamp(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                datetime.fromisoformat(v)
            except ValueError:
                raise ValueError(f"timestamp must be ISO 8601, got '{v}'")
        return v


def _generate_id() -> str:
    """Generate a short, URL-safe job ID."""
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    """Current time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _validate_webhook_url(url: str) -> None:
    """Validate a webhook URL for safety (SSRF protection)."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(
            "Webhook URL must use HTTPS"
        )
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL must have a hostname")
    # Block obvious private/reserved IPs.
    # Note: DNS rebinding (domain resolving to private IP) is not caught here;
    # that would require resolution-time checks in the HTTP client.
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(
                "Webhook URL must not target private or internal addresses"
            )
    except ValueError as exc:
        # Re-raise our own validation errors
        if "private" in str(exc) or "HTTPS" in str(exc):
            raise
        # hostname is a domain name, not an IP — that's fine


class CronJob(BaseModel):
    """A single scheduled job definition."""

    id: str = Field(default_factory=_generate_id)
    name: str
    schedule: Schedule
    target: Literal["main", "isolated"] = "isolated"
    payload: Payload
    delivery: Delivery = Field(default_factory=lambda: Delivery(mode="none"))
    enabled: bool = True
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    source: Literal["user", "yaml_import"] = "user"
