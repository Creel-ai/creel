"""Draft daemon API contracts for transport handlers and clients."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CronTarget = Literal["main", "isolated"]

StreamEventType = Literal[
    "start",
    "token",
    "tool_call",
    "tool_result",
    "final",
    "error",
]


class SendMessageRequest(BaseModel):
    """Send-message request payload."""

    sender_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    session_id: str | None = None
    auto_approve: bool = False


class SendMessageResponse(BaseModel):
    """Non-streaming response payload."""

    sender_id: str
    text: str
    session_id: str | None = None


class SessionRequest(BaseModel):
    """Request body with sender identifier."""

    sender_id: str = Field(min_length=1, max_length=128)


class SessionSummary(BaseModel):
    """Summary of a persisted conversation session."""

    session_id: str
    sender_id: str
    title: str = ""
    created_at: float
    last_active: float
    message_count: int


class SessionHistoryResponse(BaseModel):
    """Session history payload."""

    sender_id: str
    session_id: str
    messages: list[dict[str, Any]]


class DaemonStatusResponse(BaseModel):
    """Daemon runtime status payload."""

    started_at: float
    uptime_seconds: int
    sessions: dict[str, int]
    scheduler: dict[str, bool]
    channels: list[dict[str, Any]]


class StreamEvent(BaseModel):
    """Streaming event envelope for SSE/WebSocket transports."""

    type: StreamEventType
    sender_id: str
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# --- Cron job contracts ---


class CreateCronJobRequest(BaseModel):
    """Request payload for creating a cron job."""

    name: str = Field(min_length=1, max_length=256)
    schedule: dict[str, Any]
    target: CronTarget = "isolated"
    payload: dict[str, Any]
    delivery: dict[str, Any] | None = None
    enabled: bool = True


class UpdateCronJobRequest(BaseModel):
    """Request payload for updating a cron job (partial update)."""

    name: str | None = None
    schedule: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    enabled: bool | None = None


class CronJobResponse(BaseModel):
    """Response payload for a single cron job."""

    id: str
    name: str
    schedule: dict[str, Any]
    target: str
    payload: dict[str, Any]
    delivery: dict[str, Any]
    enabled: bool
    created_at: str
    updated_at: str


class RunRecordResponse(BaseModel):
    """Response payload for a single run record."""

    job_id: str
    started_at: str
    ended_at: str | None
    status: str
    error: str | None
