"""Data models for the sub-agent system."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class SubAgentStatus(str, enum.Enum):
    """Lifecycle status of a sub-agent."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"


class SubAgentConfig(BaseModel):
    """Parameters for spawning a sub-agent."""

    task: str
    label: str = ""
    model: str | None = None  # None = inherit parent model
    timeout_seconds: int = Field(default=300, ge=10, le=3600)


class SubAgentInfo(BaseModel):
    """Status snapshot of a sub-agent."""

    id: str
    label: str
    status: SubAgentStatus
    sender_id: str = ""  # parent sender who spawned this agent
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    result_summary: str = ""
    error: str = ""
