"""Channel interfaces and message types for agent communication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attachment:
    """A media attachment on a message."""

    data: bytes
    filename: str
    mime_type: str | None = None
    size: int | None = None


@dataclass
class IncomingMessage:
    """A message received from a channel."""

    sender_id: str
    text: str
    channel: str
    group_id: str | None = None
    media: list[Attachment] | None = None
    reply_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutgoingMessage:
    """A message to send through a channel."""

    recipient: str
    text: str
    media: list[Attachment] | None = None
    reply_to: str | None = None


# Type alias for the legacy callback signature: (sender_id, text) -> response_text
LegacyCallback = Callable[[str, str], str]


class Channel(ABC):
    """Base class for communication channels."""

    _stop_requested: bool = False

    @abstractmethod
    def listen(self, callback: LegacyCallback) -> None:
        """Listen for incoming messages.

        Args:
            callback: Function that takes (sender_id, text) and returns response text.
        """

    @abstractmethod
    def send(self, recipient: str, text: str) -> None:
        """Send a message to a recipient."""

    def send_message(self, msg: OutgoingMessage) -> None:
        """Send a structured outgoing message.

        Default implementation delegates to ``send()`` for backward compatibility.
        Channels can override to handle media attachments and other fields.
        """
        self.send(msg.recipient, msg.text)

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
        return {
            "channel": type(self).__name__,
            "healthy": not self._stop_requested,
        }
