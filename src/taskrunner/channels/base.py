"""Channel interfaces for agent communication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable


class Channel(ABC):
    """Base class for communication channels."""

    _stop_requested: bool = False

    @abstractmethod
    def listen(self, callback: Callable[[str, str], str]) -> None:
        """Listen for incoming messages.

        Args:
            callback: Function that takes (sender_id, text) and returns response text.
        """

    @abstractmethod
    def send(self, recipient: str, text: str) -> None:
        """Send a message to a recipient."""

    def stop(self) -> None:
        """Request the channel to stop listening."""
        self._stop_requested = True

    def get_webhook_routes(self) -> list[dict[str, Any]] | None:
        """Return webhook route definitions for this channel, if any.

        Each dict should contain ``path``, ``method``, and ``handler`` keys.
        Returns None if the channel does not use webhooks.
        """
        return None

    def wait_for_reply(self, sender_id: str, timeout_seconds: int = 60) -> str | None:
        """Wait for a reply from a specific sender within a timeout.

        Returns the reply text, or None if not supported or timeout reached.
        """
        return None

    def health_check(self) -> dict[str, Any]:
        """Return health status for this channel."""
        return {"healthy": not self._stop_requested}
