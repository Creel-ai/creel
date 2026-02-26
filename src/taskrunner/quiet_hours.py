"""Quiet hours logic for suppressing proactive notifications during specified time windows."""

from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from taskrunner.models import QuietHoursConfig

logger = logging.getLogger(__name__)


def is_quiet_hours(config: QuietHoursConfig) -> bool:
    """Check if current time is within the quiet hours window.

    Args:
        config: Quiet hours configuration

    Returns:
        True if current time is within quiet hours, False otherwise
    """
    if not config.enabled:
        return False

    try:
        # Parse start and end times
        start_time = time.fromisoformat(config.start)
        end_time = time.fromisoformat(config.end)

        # Get current time in the configured timezone
        tz = ZoneInfo(config.timezone)
        now = datetime.now(tz).time()

        # Handle overnight ranges (e.g., 23:00 → 08:00)
        if start_time > end_time:
            # Quiet hours cross midnight
            return now >= start_time or now < end_time
        else:
            # Quiet hours within same day
            return start_time <= now < end_time

    except Exception as e:
        logger.error("Error checking quiet hours: %s", e)
        # Default to not quiet on error
        return False


def should_suppress(config: QuietHoursConfig, urgent: bool = False) -> bool:
    """Determine if a message should be suppressed based on quiet hours settings.

    Args:
        config: Quiet hours configuration
        urgent: Whether the message is marked as urgent

    Returns:
        True if message should be suppressed, False if it should be sent
    """
    if not config.enabled:
        return False

    # Allow urgent messages if configured
    if urgent and config.allow_urgent:
        return False

    return is_quiet_hours(config)
