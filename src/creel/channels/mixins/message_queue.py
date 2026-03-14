"""MessageQueueMixin — outbound message queuing with rate limiting."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class _Sendable(Protocol):
    """Protocol for objects that have a ``send(recipient, text)`` method."""

    def send(self, recipient: str, text: str) -> None: ...


@dataclass
class QueuedMessage:
    """A message waiting to be sent."""

    recipient: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class MessageQueueMixin:
    """Mixin that buffers outbound messages and sends them with rate limiting.

    The host class **must** provide a ``send(recipient, text)`` method (see
    :class:`_Sendable`).  This is enforced at init-subclass time.

    This mixin is **synchronous** — ``_flush_queue`` and ``_rate_limited_send``
    use ``time.sleep`` for rate limiting.  Do not call them from an async
    event loop without wrapping in ``asyncio.to_thread``.

    Class attributes (override on the subclass or instance):

    - ``_rate_limit_delay``: minimum seconds between consecutive sends (0 = no limit).
    - ``_max_queue_size``: drop oldest messages when the queue exceeds this size (0 = unlimited).
    """

    _rate_limit_delay: float = 0.0
    _max_queue_size: int = 0

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def _get_queue(self) -> deque[QueuedMessage]:
        """Lazily initialise and return the outbound queue."""
        if not hasattr(self, "_message_queue"):
            self._message_queue: deque[QueuedMessage] = deque()
            self._last_send_time: float = 0.0
        return self._message_queue

    def _enqueue(self, recipient: str, text: str, **metadata: Any) -> None:
        """Add a message to the outbound queue."""
        q = self._get_queue()
        if self._max_queue_size and len(q) >= self._max_queue_size:
            dropped = q.popleft()
            logger.warning(
                "Queue full (%d) — dropped oldest message to %s",
                self._max_queue_size,
                dropped.recipient,
            )
        q.append(QueuedMessage(recipient=recipient, text=text, metadata=metadata))

    def _flush_queue(self) -> int:
        """Send all queued messages respecting the rate limit.

        Returns the number of messages sent.
        """
        q = self._get_queue()
        sent = 0
        while q:
            msg = q.popleft()
            self._rate_limited_send(msg.recipient, msg.text)
            sent += 1
        return sent

    def _rate_limited_send(self, recipient: str, text: str) -> None:
        """Send a single message, respecting ``_rate_limit_delay``."""
        if self._rate_limit_delay > 0:
            if not hasattr(self, "_last_send_time"):
                self._last_send_time = 0.0
            elapsed = time.monotonic() - self._last_send_time
            if elapsed < self._rate_limit_delay:
                time.sleep(self._rate_limit_delay - elapsed)

        send = getattr(self, "send", None)
        if send is None:
            raise TypeError(
                f"{type(self).__name__} uses MessageQueueMixin but has no send() method"
            )
        send(recipient, text)
        self._last_send_time = time.monotonic()

    @property
    def queue_size(self) -> int:
        """Current number of messages waiting to be sent."""
        return len(self._get_queue())
