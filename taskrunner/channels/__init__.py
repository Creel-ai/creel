"""Channel interfaces for agent communication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class Channel(ABC):
    """Base class for communication channels."""

    @abstractmethod
    def listen(self, callback: Callable[[str, str], str]) -> None:
        """Listen for incoming messages.

        Args:
            callback: Function that takes (sender_id, text) and returns response text.
        """

    @abstractmethod
    def send(self, recipient: str, text: str) -> None:
        """Send a message to a recipient."""
