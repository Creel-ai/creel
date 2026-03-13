"""RetryMixin — exponential backoff for API calls."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryMixin:
    """Mixin providing exponential backoff retry for arbitrary callables.

    Class attributes (override on the subclass or instance as needed):

    - ``_max_retries``: maximum number of attempts (default 3).
    - ``_retry_base_delay``: initial delay in seconds (default 1.0).
    - ``_retry_max_delay``: ceiling for the backoff delay (default 60.0).
    """

    _max_retries: int = 3
    _retry_base_delay: float = 1.0
    _retry_max_delay: float = 60.0

    def _retry_with_backoff(
        self,
        fn: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute *fn* with exponential backoff on transient failures.

        Returns the result of *fn* on success.  Raises the last exception
        if all attempts are exhausted.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    delay = min(
                        self._retry_base_delay * (2**attempt),
                        self._retry_max_delay,
                    )
                    logger.warning(
                        "%s retry %d/%d after %.1fs: %s",
                        type(self).__name__,
                        attempt + 1,
                        self._max_retries,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _calculate_backoff(
        base_interval: float,
        consecutive_errors: int,
        max_backoff: float = 60.0,
    ) -> float:
        """Calculate exponential backoff delay.

        This is the same formula used by all polling channels::

            min(base_interval * 2^consecutive_errors, max_backoff)
        """
        return min(base_interval * (2**consecutive_errors), max_backoff)
