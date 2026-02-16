"""Channel interfaces for agent communication."""

from taskrunner.channels.base import Channel
from taskrunner.channels.plugin import ChannelCapability, ChannelPluginMeta
from taskrunner.channels.registry import ChannelRegistry

__all__ = ["Channel", "ChannelCapability", "ChannelPluginMeta", "ChannelRegistry"]
