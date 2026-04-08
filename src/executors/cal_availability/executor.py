#!/usr/bin/env python3
"""Calendar Availability Finder executor.

Queries Google Calendar for events in a date range and finds free time slots
of at least a specified duration. Supports working-hours-only filtering.

Requires GOOGLE_ACCESS_TOKEN env var containing a short-lived OAuth2 access token.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta

from googleapiclient.discovery import build

try:
    from executors.google_creds import get_credentials
except ModuleNotFoundError:
    from google_creds import get_credentials  # type: ignore[no-redef]

# Default working hours (configurable via env vars)
DEFAULT_WORK_START = 9  # 9 AM
DEFAULT_WORK_END = 17  # 5 PM
# Monday=0 .. Friday=4
WORKDAYS = {0, 1, 2, 3, 4}


def register_skill():
    """Register the cal_availability skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="cal_availability",
        label="Calendar Availability",
        tools=(
            ToolSpec(
                name="find_free_time",
                description=(
                    "Find free time slots on Google Calendar. "
                    "Queries events in a date range and returns available slots "
                    "of at least the requested duration."
                ),
                params=(
                    Param(
                        name="date_range",
                        type="string",
                        description=(
                            "Date range to search, e.g. 'today', 'tomorrow', "
                            "'this week', 'next 3 days', or 'YYYY-MM-DD to YYYY-MM-DD'"
                        ),
                        required=True,
                    ),
                    Param(
                        name="duration_minutes",
                        type="number",
                        description="Minimum slot duration in minutes (default: 30)",
                    ),
                    Param(
                        name="working_hours_only",
                        type="boolean",
                        description=(
                            "Only show slots during working hours (Mon-Fri 9am-5pm, default: true)"
                        ),
                    ),
                ),
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        date_range = config.args.get("date_range", "today")
        duration_minutes = int(config.args.get("duration_minutes", 30))
        working_hours_only = config.args.get("working_hours_only", True)
        # Handle string "false"/"true" from LLM
        if isinstance(working_hours_only, str):
            working_hours_only = working_hours_only.lower() != "false"
        result = find_free_time(date_range, duration_minutes, working_hours_only)
        return json.dumps(result, indent=2)

    return meta, execute


def _parse_date_range(date_range: str) -> tuple[datetime, datetime]:
    """Parse a human-friendly date range into (start, end) datetimes in UTC."""
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    dr = date_range.strip().lower()

    if dr == "today":
        return today_start, today_start + timedelta(days=1)
    elif dr == "tomorrow":
        tomorrow = today_start + timedelta(days=1)
        return tomorrow, tomorrow + timedelta(days=1)
    elif dr == "this week":
        # Monday of current week through end of Friday
        monday = today_start - timedelta(days=now.weekday())
        # If already past Monday, start from today
        start = max(monday, today_start)
        end = monday + timedelta(days=7)
        return start, end
    elif dr == "next week":
        next_monday = today_start + timedelta(days=(7 - now.weekday()))
        return next_monday, next_monday + timedelta(days=7)
    elif dr.startswith("next ") and dr.endswith(" days"):
        try:
            n = int(dr.split()[1])
        except (ValueError, IndexError):
            n = 3
        return today_start, today_start + timedelta(days=n)
    elif " to " in dr:
        # "YYYY-MM-DD to YYYY-MM-DD"
        parts = dr.split(" to ")
        start = datetime.strptime(parts[0].strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        end = datetime.strptime(parts[1].strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        end = end + timedelta(days=1)  # Include the end date
        return start, end
    else:
        # Default: next 3 days
        return today_start, today_start + timedelta(days=3)


def _fetch_events(time_min: datetime, time_max: datetime) -> list[dict]:
    """Fetch calendar events from Google Calendar API."""
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        )
        .execute()
    )

    events = []
    for event in events_result.get("items", []):
        # Skip all-day events — they don't block specific time slots
        if "date" in event["start"]:
            continue
        start = event["start"].get("dateTime", "")
        end = event["end"].get("dateTime", "")
        if start and end:
            events.append(
                {
                    "start": datetime.fromisoformat(start),
                    "end": datetime.fromisoformat(end),
                    "summary": event.get("summary", "(No title)"),
                }
            )
    return events


def _find_gaps(
    events: list[dict],
    range_start: datetime,
    range_end: datetime,
    min_duration: timedelta,
    working_hours_only: bool,
    work_start: int = DEFAULT_WORK_START,
    work_end: int = DEFAULT_WORK_END,
) -> list[dict]:
    """Find free time gaps between events within the given range."""
    # Sort events by start time
    events = sorted(events, key=lambda e: e["start"])

    # Generate candidate windows per day
    slots: list[tuple[datetime, datetime]] = []
    current_day = range_start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = range_end

    while current_day < end_day:
        if working_hours_only:
            if current_day.weekday() not in WORKDAYS:
                current_day += timedelta(days=1)
                continue
            day_start = current_day.replace(hour=work_start, minute=0, second=0)
            day_end = current_day.replace(hour=work_end, minute=0, second=0)
        else:
            day_start = current_day
            day_end = current_day + timedelta(days=1)

        # Clamp to overall range
        day_start = max(day_start, range_start)
        day_end = min(day_end, range_end)

        if day_start < day_end:
            slots.append((day_start, day_end))

        current_day = current_day.replace(hour=0, minute=0, second=0) + timedelta(days=1)

    # For each candidate window, subtract busy times
    free_slots: list[dict] = []
    for window_start, window_end in slots:
        # Collect events overlapping this window
        busy = []
        for ev in events:
            if ev["end"] > window_start and ev["start"] < window_end:
                busy.append((max(ev["start"], window_start), min(ev["end"], window_end)))

        # Merge overlapping busy intervals
        busy.sort()
        merged: list[tuple[datetime, datetime]] = []
        for b_start, b_end in busy:
            if merged and b_start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b_end))
            else:
                merged.append((b_start, b_end))

        # Extract free gaps
        cursor = window_start
        for b_start, b_end in merged:
            if b_start > cursor:
                gap = b_start - cursor
                if gap >= min_duration:
                    free_slots.append(
                        {
                            "start": cursor.isoformat(),
                            "end": b_start.isoformat(),
                            "duration_minutes": int(gap.total_seconds() / 60),
                        }
                    )
            cursor = max(cursor, b_end)

        # Trailing gap after last event
        if cursor < window_end:
            gap = window_end - cursor
            if gap >= min_duration:
                free_slots.append(
                    {
                        "start": cursor.isoformat(),
                        "end": window_end.isoformat(),
                        "duration_minutes": int(gap.total_seconds() / 60),
                    }
                )

    return free_slots


def find_free_time(
    date_range: str = "today",
    duration_minutes: int = 30,
    working_hours_only: bool = True,
) -> dict:
    """Find free time slots in the specified date range.

    Returns a dict with query info and a list of free slots.
    """
    range_start, range_end = _parse_date_range(date_range)
    min_duration = timedelta(minutes=duration_minutes)

    work_start = int(os.environ.get("WORK_HOURS_START", DEFAULT_WORK_START))
    work_end = int(os.environ.get("WORK_HOURS_END", DEFAULT_WORK_END))

    events = _fetch_events(range_start, range_end)
    free_slots = _find_gaps(
        events,
        range_start,
        range_end,
        min_duration,
        working_hours_only,
        work_start,
        work_end,
    )

    return {
        "date_range": date_range,
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "duration_minutes": duration_minutes,
        "working_hours_only": working_hours_only,
        "total_free_slots": len(free_slots),
        "free_slots": free_slots,
    }


def main() -> None:
    date_range = os.environ.get("DATE_RANGE", "today")
    duration_minutes = int(os.environ.get("DURATION_MINUTES", "30"))
    working_hours_only = os.environ.get("WORKING_HOURS_ONLY", "true").lower() != "false"

    if len(sys.argv) > 1:
        date_range = sys.argv[1]
    if len(sys.argv) > 2:
        duration_minutes = int(sys.argv[2])
    if len(sys.argv) > 3:
        working_hours_only = sys.argv[3].lower() != "false"

    try:
        result = find_free_time(date_range, duration_minutes, working_hours_only)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
