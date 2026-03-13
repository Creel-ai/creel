"""Built-in monitor templates for common use cases."""

from __future__ import annotations

from creel.cron.models import Delivery, Schedule
from creel.monitors.models import AlertLevel, Monitor, QuietHours

TEMPLATES: dict[str, dict] = {
    "urgent_email": {
        "name": "Urgent Email Monitor",
        "description": "Check for urgent unread emails that need immediate attention",
        "schedule": Schedule(kind="cron", expr="*/15 * * * *"),
        "executor": "gmail_readonly",
        "prompt": (
            "Check for urgent unread emails. Look for emails that are:\n"
            "- Marked as important or high priority\n"
            "- From known VIP senders (boss, direct reports, key clients)\n"
            "- Containing urgent keywords (urgent, ASAP, emergency, deadline today)\n"
            "- Calendar invitations requiring response within 24 hours\n\n"
            "If you find urgent emails, respond with a brief summary of each one "
            "(sender, subject, why it's urgent). If nothing urgent, respond with "
            "an empty string."
        ),
        "alert_level": AlertLevel.URGENT,
        "quiet_hours": QuietHours(start="23:00", end="07:00"),
        "cooldown_seconds": 1800,  # 30 minutes
    },
    "calendar_conflicts": {
        "name": "Calendar Conflict Detector",
        "description": "Check for double-bookings and scheduling conflicts",
        "schedule": Schedule(kind="cron", expr="0 8 * * *"),
        "executor": "gcal",
        "prompt": (
            "Check today's and tomorrow's calendar for scheduling conflicts:\n"
            "- Double-booked time slots (overlapping meetings)\n"
            "- Back-to-back meetings with no buffer\n"
            "- Meetings that conflict with focus time or lunch blocks\n"
            "- Events missing location or video link\n\n"
            "If you find conflicts, list each one with the affected events, "
            "times, and a suggested resolution. If no conflicts, respond with "
            "an empty string."
        ),
        "alert_level": AlertLevel.NOTICE,
        "quiet_hours": QuietHours(start="22:00", end="07:00"),
        "cooldown_seconds": 3600,  # 1 hour
    },
    "system_health": {
        "name": "System Health Check",
        "description": "Monitor disk space, memory, and system resources",
        "schedule": Schedule(kind="cron", expr="0 */6 * * *"),
        "executor": "exec",
        "prompt": (
            "Run basic system health checks:\n"
            "- Check disk space usage (alert if any partition > 85%)\n"
            "- Check available memory (alert if < 500MB free)\n"
            "- Check system load average (alert if > number of CPU cores)\n"
            "- Check for zombie processes\n\n"
            "Report any issues found with specific values and thresholds. "
            "If everything is healthy, respond with an empty string."
        ),
        "alert_level": AlertLevel.NOTICE,
        "quiet_hours": None,
        "cooldown_seconds": 7200,  # 2 hours
    },
}


def get_template(name: str) -> dict | None:
    """Return a template by name, or None if not found."""
    return TEMPLATES.get(name)


def list_templates() -> list[str]:
    """Return all available template names."""
    return list(TEMPLATES.keys())


def create_from_template(
    template_name: str,
    *,
    delivery: Delivery | None = None,
    quiet_hours_tz: str | None = None,
) -> Monitor:
    """Create a Monitor instance from a named template.

    Args:
        template_name: Name of the template to use.
        delivery: Override delivery config (default: none).
        quiet_hours_tz: Override timezone for quiet hours.

    Raises:
        KeyError: If template not found.
    """
    tmpl = TEMPLATES.get(template_name)
    if tmpl is None:
        raise KeyError(
            f"Unknown template '{template_name}'. Available: {', '.join(TEMPLATES.keys())}"
        )

    kwargs = dict(tmpl)
    if delivery is not None:
        kwargs["delivery"] = delivery
    else:
        kwargs.setdefault("delivery", Delivery(mode="none"))

    if quiet_hours_tz and kwargs.get("quiet_hours"):
        qh = kwargs["quiet_hours"]
        kwargs["quiet_hours"] = QuietHours(start=qh.start, end=qh.end, timezone=quiet_hours_tz)

    return Monitor(**kwargs)
