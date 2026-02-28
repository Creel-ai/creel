"""Channel interfaces for agent communication."""

from taskrunner.channels.base import (
    Attachment,
    Channel,
    IncomingMessage,
    LegacyCallback,
    OutgoingMessage,
)
from taskrunner.channels.plugin import ChannelCapability, ChannelPluginMeta
from taskrunner.channels.registry import ChannelRegistry

__all__ = [
    "Attachment",
    "Channel",
    "ChannelCapability",
    "ChannelPluginMeta",
    "ChannelRegistry",
    "IncomingMessage",
    "LegacyCallback",
    "OutgoingMessage",
]
