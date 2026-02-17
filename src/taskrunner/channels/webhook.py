"""Webhook mixin for channels that receive messages via HTTP callbacks."""

from __future__ import annotations

import time
from typing import Callable


class WebhookChannelMixin:
    """Mixin for channels that receive messages through webhook HTTP handlers.

    Subclasses should override ``get_webhook_routes()`` on the Channel class
    and use this mixin to store the message callback and block in ``listen()``.
    """

    _webhook_callback: Callable[[str, str], str] | None = None

    def set_webhook_callback(self, callback: Callable[[str, str], str]) -> None:
        """Store the message callback for use by webhook handlers."""
        self._webhook_callback = callback

    def _webhook_listen_block(self) -> None:
        """Block the listener thread until stop is requested.

        Call this from ``listen()`` after setting up the callback so the
        daemon thread stays alive while webhooks are handled by FastAPI.
        """
        while not self._stop_requested:
            time.sleep(1)
