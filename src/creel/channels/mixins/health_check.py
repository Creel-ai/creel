"""HealthCheckMixin — channel health monitoring and reconnection."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class HealthCheckMixin:
    """Mixin for standardised channel health monitoring.

    Provides helpers that channels can call from their ``health_check()``
    method.  Supports both standalone channels and bridge-backed channels.

    Tracks the last successful health check timestamp for reconnection logic.
    """

    _stop_requested: bool = False
    _last_healthy_time: float = 0.0
    _consecutive_health_failures: int = 0

    def _basic_health(self) -> dict[str, Any]:
        """Return minimal health status for a standalone channel."""
        healthy = not self._stop_requested
        if healthy:
            self._last_healthy_time = time.monotonic()
            self._consecutive_health_failures = 0
        return {
            "channel": type(self).__name__,
            "healthy": healthy,
        }

    def _bridge_health(self) -> dict[str, Any]:
        """Return health status combining channel state with bridge health.

        Expects ``self._bridge`` to have a ``health()`` method.  Falls back
        gracefully if the bridge is missing or has no such method.
        """
        bridge = getattr(self, "_bridge", None)
        bridge_health: dict[str, Any] = {}
        if bridge and hasattr(bridge, "health"):
            try:
                bridge_health = bridge.health()
            except Exception:
                logger.warning("Bridge health check failed", exc_info=True)
                bridge_health = {"healthy": False, "error": "health check failed"}

        healthy = not self._stop_requested and bridge_health.get("healthy", False)
        if healthy:
            self._last_healthy_time = time.monotonic()
            self._consecutive_health_failures = 0
        else:
            self._consecutive_health_failures += 1

        mode = getattr(self, "_mode", "unknown")
        return {
            "channel": type(self).__name__,
            "healthy": healthy,
            "mode": mode,
            "bridge": bridge_health,
        }

    def _should_reconnect(self, unhealthy_threshold: float = 120.0) -> bool:
        """Return ``True`` if the channel has been unhealthy long enough to reconnect.

        Parameters
        ----------
        unhealthy_threshold:
            Seconds of continuous unhealthiness before suggesting reconnection.
        """
        if self._last_healthy_time == 0.0:
            return False
        elapsed = time.monotonic() - self._last_healthy_time
        return elapsed > unhealthy_threshold


class BridgeClientMixin(HealthCheckMixin):
    """Convenience subclass for channels that delegate I/O to a bridge server.

    Provides ``_bridge_health_check()`` which returns the same dict shape that
    ``BridgeClientMixin`` in the legacy ``mixins`` module produced, for
    backward compatibility.
    """

    _mode: str = "polling"

    def _bridge_health_check(self) -> dict[str, Any]:
        """Return health info combining channel and bridge status."""
        return self._bridge_health()
