#!/usr/bin/env python3
"""Google Calendar executor - retrieves today's events.

Requires GOOGLE_ACCESS_TOKEN env var containing a short-lived OAuth2 access token.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

try:
    from executors.google_creds import get_credentials
except ModuleNotFoundError:
    from google_creds import get_credentials


def fetch_events(range_arg: str = "today") -> list[dict]:
    """Fetch calendar events for the specified range."""
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.now(timezone.utc)

    if range_arg == "today":
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)
    elif range_arg == "week":
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=7)
    else:
        # Default to today
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )

    events = []
    for event in events_result.get("items", []):
        start = event["start"].get("dateTime", event["start"].get("date", ""))
        end = event["end"].get("dateTime", event["end"].get("date", ""))

        events.append(
            {
                "summary": event.get("summary", "(No title)"),
                "start": start,
                "end": end,
                "location": event.get("location", ""),
                "description": event.get("description", ""),
                "all_day": "date" in event["start"],
            }
        )

    return events


def main() -> None:
    range_arg = os.environ.get("RANGE", "today")
    if len(sys.argv) > 1:
        range_arg = sys.argv[1]

    try:
        events = fetch_events(range_arg)
        print(json.dumps(events, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
