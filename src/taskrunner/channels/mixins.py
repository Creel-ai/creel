"""Reusable mixins that eliminate boilerplate in channel implementations."""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from typing import Any

from taskrunner.channels.base import IncomingMessage, LegacyCallback

logger = logging.getLogger(__name__)


class PollingChannelMixin:
    """Mixin that provides a standard polling loop with backoff.

    Subclasses implement ``_poll_once()`` to fetch new messages.  The mixin
    handles the loop, sleep, error backoff, and stop-flag checking.

    Attributes expected on ``self`` (provided by ``Channel``):
        _stop_requested: bool
    """

    _poll_interval: int | float = 5
    _max_backoff: int | float = 60
    _stop_requested: bool = False

    @abstractmethod
    def _poll_once(self) -> list[IncomingMessage]:
        """Fetch new messages since the last poll.

        Called once per polling cycle.  Return an empty list when there is
        nothing new.  Raise on transient errors — the loop will back off.
        """

    def _run_poll_loop(self, callback: LegacyCallback) -> None:
        """Run the standard polling loop until ``_stop_requested`` is set.

        For each :class:`IncomingMessage` returned by ``_poll_once()``, the
        legacy ``(sender_id, text) -> str`` callback is invoked and the reply
        is sent via ``self.send()``.
        """
        consecutive_errors = 0

        while not self._stop_requested:
            try:
                messages = self._poll_once()
                consecutive_errors = 0

                for msg in messages:
                    response = callback(msg.sender_id, msg.text)
                    self.send(msg.sender_id, response)  # type: ignore[attr-defined]

            except Exception:
                consecutive_errors += 1
                backoff = min(
                    self._poll_interval * (2**consecutive_errors),
                    self._max_backoff,
                )
                logger.exception(
                    "%s poll error (consecutive=%d, backoff=%.1fs)",
                    type(self).__name__,
                    consecutive_errors,
                    backoff,
                )
                if not self._stop_requested:
                    time.sleep(backoff)
                continue

            if not self._stop_requested:
                time.sleep(self._poll_interval)


class BridgeClientMixin:
    """Mixin for channels that delegate I/O to a bridge server.

    Provides standard health-check aggregation that combines channel state
    with bridge health.
    """

    _stop_requested: bool = False
    _mode: str = "polling"

    def _bridge_health_check(self) -> dict[str, Any]:
        """Return health info combining channel and bridge status."""
        bridge = getattr(self, "_bridge", None)
        bridge_health = bridge.health() if bridge and hasattr(bridge, "health") else {}
        return {
            "channel": type(self).__name__,
            "healthy": not self._stop_requested and bridge_health.get("healthy", False),
            "mode": self._mode,
            "bridge": bridge_health,
        }
