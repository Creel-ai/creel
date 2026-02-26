"""Incoming message models with media attachment support."""

from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, Field


class AttachmentType(str, enum.Enum):
    """Type of media attachment."""

    IMAGE = "image"
    VOICE = "voice"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"


class Attachment(BaseModel):
    """A media attachment on an incoming message."""

    type: AttachmentType
    file_path: Path | None = None
    url: str | None = None
    mime_type: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    data: bytes | None = Field(default=None, exclude=True, repr=False)


class IncomingMessage(BaseModel):
    """Unified incoming message from any channel."""

    sender_id: str
    text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    channel: str | None = None
