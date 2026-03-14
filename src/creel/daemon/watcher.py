"""File watcher for automatic config reload.

Uses simple mtime polling to avoid adding external dependencies.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 2.0  # seconds


class ConfigWatcher:
    """Polls a config file for modifications and invokes a callback on change.

    Uses file mtime comparison to detect changes — simple, cross-platform,
    and dependency-free.
    """

    def __init__(
        self,
        config_path: Path,
        on_change: Callable[[], None],
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._path = config_path
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_mtime: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the watcher in a background daemon thread."""
        if self.running:
            return

        # Capture the initial mtime so we don't fire on startup.
        self._last_mtime = self._get_mtime()
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._poll_loop,
            name="creel-config-watcher",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Config watcher started for %s (poll every %.1fs)", self._path, self._poll_interval
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the watcher to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("Config watcher stopped")

    def _get_mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._poll_interval)
            if self._stop_event.is_set():
                break

            current_mtime = self._get_mtime()
            if current_mtime is None:
                continue

            if self._last_mtime is not None and current_mtime != self._last_mtime:
                logger.info("Config file change detected: %s", self._path)
                self._last_mtime = current_mtime
                try:
                    self._on_change()
                except Exception:
                    logger.exception("Config reload callback failed")
            elif self._last_mtime is None:
                # File appeared for the first time
                self._last_mtime = current_mtime
