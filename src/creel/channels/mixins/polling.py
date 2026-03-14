"""PollingChannelMixin — standard polling loop with backoff."""

from __future__ import annotations

import logging
import time
from abc import abstractmethod

from creel.channels.base import IncomingMessage, LegacyCallback
from creel.channels.mixins.retry import RetryMixin

logger = logging.getLogger(__name__)


class PollingChannelMixin:
    """Mixin that provides a standard polling loop with backoff.

    Subclasses implement ``_poll_once()`` to fetch new messages.  The mixin
    handles the loop, sleep, error backoff, and stop-flag checking.

    Attributes expected on ``self`` (provided by ``Channel``):
        _stop_requested: bool
    """

    #: Seconds between successful polls.  Also used as the base for exponential
    #: backoff on errors: ``poll_interval * 2^consecutive_errors``, capped at
    #: ``_max_backoff``.
    _poll_interval: int | float = 5
    _max_backoff: int | float = 60
    _stop_requested: bool = False

    @abstractmethod
    def _poll_once(self) -> list[IncomingMessage]:
        """Fetch new messages since the last poll.

        Called once per polling cycle.  Return an empty list when there is
        nothing new.  Raise on transient errors — the loop will back off.
        """

    def _before_dispatch(self, msg: IncomingMessage) -> None:
        """Hook called for each message right before the callback runs.

        Override to send typing indicators, log, etc.  The default is a no-op.
        """

    def _dispatch_message(self, msg: IncomingMessage, callback: LegacyCallback) -> str:
        """Dispatch a single message to the callback and return the response.

        The default calls ``callback(sender_id, text)``.  Channels that need
        to pass richer data (e.g. media attachments) can override this method.
        """
        return callback(msg.sender_id, msg.text)

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
                    self._before_dispatch(msg)
                    response = self._dispatch_message(msg, callback)
                    self.send(msg.sender_id, response)  # type: ignore[attr-defined]

            except Exception:
                consecutive_errors += 1
                backoff = RetryMixin._calculate_backoff(
                    self._poll_interval, consecutive_errors, self._max_backoff
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
