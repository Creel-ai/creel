"""Channel plugin metadata and capability declarations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Flag, auto

from pydantic import BaseModel


class ChannelCapability(Flag):
    """Capabilities a channel plugin can declare."""

    POLLING = auto()
    WEBHOOK = auto()
    SEND = auto()
    MEDIA = auto()
    REACTIONS = auto()
    READ_RECEIPTS = auto()
    TYPING_INDICATOR = auto()
    GROUP_CHAT = auto()
    WAIT_FOR_REPLY = auto()


@dataclass(frozen=True)
class ChannelPluginMeta:
    """Immutable metadata describing a channel plugin.

    Attributes:
        id: Unique identifier (e.g. "imessage", "whatsapp").
        label: Human-readable name.
        capabilities: Set of declared capabilities.
        config_schema: Pydantic model class for channel-specific config.
        priority: Lower = loaded first when multiple channels compete.
        platform: OS constraint (e.g. "darwin"); None means any platform.
        extras: pip extras required to use this channel (e.g. ["whatsapp"]).
    """

    id: str
    label: str
    capabilities: ChannelCapability
    config_schema: type[BaseModel] | None = None
    priority: int = 100
    platform: str | None = None
    extras: list[str] = field(default_factory=list)
