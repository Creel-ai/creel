"""Channel interfaces for agent communication."""

from creel.channels.base import Channel
from creel.channels.plugin import ChannelCapability, ChannelPluginMeta
from creel.channels.registry import ChannelRegistry

__all__ = ["Channel", "ChannelCapability", "ChannelPluginMeta", "ChannelRegistry"]
