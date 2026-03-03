"""Channel plugin registry — discovers and instantiates channel plugins."""

from __future__ import annotations

import importlib.metadata
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from creel.channels.base import Channel
from creel.channels.plugin import ChannelPluginMeta

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "creel.channels"


@dataclass
class _ChannelEntry:
    """Internal record for a registered channel plugin."""

    meta: ChannelPluginMeta
    factory: Callable[[dict[str, Any]], Channel]


class ChannelRegistry:
    """Discovers, registers, and instantiates channel plugins."""

    def __init__(self) -> None:
        self._entries: dict[str, _ChannelEntry] = {}

    def register(
        self,
        meta: ChannelPluginMeta,
        factory: Callable[[dict[str, Any]], Channel],
    ) -> None:
        """Register a channel plugin by its metadata and factory function."""
        if meta.id in self._entries:
            logger.warning("Overwriting channel plugin '%s'", meta.id)
        self._entries[meta.id] = _ChannelEntry(meta=meta, factory=factory)
        logger.info("Registered channel plugin '%s'", meta.id)

    # Built-in channel modules (used as fallback when entry points are
    # unavailable, e.g. when PYTHONPATH shadows the installed package).
    _BUILTIN_CHANNELS: list[str] = [
        "creel.channels.imessage",
        "creel.channels.bluebubbles",
        "creel.channels.whatsapp",
        "creel.channels.telegram",
    ]

    def discover(self) -> None:
        """Scan entry points for channel plugins and register them.

        Each entry point must resolve to a ``register_plugin()`` callable
        that returns ``(ChannelPluginMeta, factory_fn)``.

        Falls back to direct imports of built-in channel modules when no
        entry points are found (e.g. PYTHONPATH-based dev setups).
        """
        eps = importlib.metadata.entry_points()
        # Python 3.12+: entry_points() returns a SelectableGroups or dict-like
        if hasattr(eps, "select"):
            channel_eps = list(eps.select(group=ENTRY_POINT_GROUP))
        else:
            channel_eps = list(eps.get(ENTRY_POINT_GROUP) or [])

        for ep in channel_eps:
            try:
                register_fn = ep.load()
                meta, factory = register_fn()
                self.register(meta, factory)
            except Exception:
                logger.debug(
                    "Failed to load channel plugin '%s' from entry point",
                    ep.name,
                    exc_info=True,
                )

        # Always attempt builtin imports for any channels not yet registered
        # (covers stale egg-info, partial entry-point discovery, PYTHONPATH setups)
        self._discover_builtins()

        if self._entries:
            logger.info(
                "Channel discovery complete: %s",
                ", ".join(sorted(self._entries.keys())),
            )
        else:
            logger.warning("Channel discovery found no plugins")

    def _discover_builtins(self) -> None:
        """Import built-in channel modules directly to fill any gaps."""
        import importlib

        for module_path in self._BUILTIN_CHANNELS:
            try:
                mod = importlib.import_module(module_path)
                register_fn = getattr(mod, "register_plugin", None)
                if register_fn is None:
                    continue
                meta, factory = register_fn()
                if meta.id not in self._entries:
                    self.register(meta, factory)
            except Exception:
                logger.debug("Could not load built-in channel %s", module_path)

    def get(self, channel_id: str) -> _ChannelEntry | None:
        """Look up a registered channel entry by ID."""
        return self._entries.get(channel_id)

    def available(self) -> list[ChannelPluginMeta]:
        """Return metadata for all registered plugins compatible with this platform."""
        platform = sys.platform
        result = []
        for entry in self._entries.values():
            if entry.meta.platform is None or entry.meta.platform == platform:
                result.append(entry.meta)
        result.sort(key=lambda m: (m.priority, m.id))
        return result

    def create_channel(self, channel_id: str, config: dict[str, Any]) -> Channel:
        """Instantiate a channel from its registered factory.

        Args:
            channel_id: Plugin ID (e.g. "imessage").
            config: Raw config dict passed to the factory function.

        Raises:
            ValueError: If the channel ID is not registered.
        """
        entry = self._entries.get(channel_id)
        if entry is None:
            known = ", ".join(sorted(self._entries.keys())) or "(none)"
            raise ValueError(f"Unknown channel '{channel_id}'. Registered: {known}")
        logger.info("Creating channel '%s'", channel_id)
        return entry.factory(config)
