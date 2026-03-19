#!/usr/bin/env python3
"""Google Calendar write executor - creates calendar events.

Requires GOOGLE_ACCESS_TOKEN env var containing a short-lived OAuth2 access token.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys

from googleapiclient.discovery import build

try:
    from executors.google_creds import get_credentials
except ModuleNotFoundError:
    from google_creds import get_credentials  # type: ignore[no-redef]


def register_skill():
    """Register the gcal_write skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="gcal_write",
        label="Google Calendar Write",
        tools=(
            ToolSpec(
                name="create_event",
                description="Create a calendar event",
                params=(
                    Param(
                        name="summary",
                        type="string",
                        description="Event title",
                        required=True,
                    ),
                    Param(
                        name="start",
                        type="string",
                        description="Start time (ISO 8601)",
                        required=True,
                    ),
                    Param(
                        name="end",
                        type="string",
                        description="End time (ISO 8601)",
                        required=True,
                    ),
                    Param(
                        name="description",
                        type="string",
                        description="Event description",
                    ),
                    Param(
                        name="location",
                        type="string",
                        description="Event location",
                    ),
                ),
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        summary = config.args.get("summary", "")
        start = config.args.get("start", "")
        end = config.args.get("end", "")
        description = config.args.get("description", "")
        location = config.args.get("location", "")
        event = create_event(summary, start, end, description, location)
        return json.dumps(event, indent=2)

    return meta, execute


def create_event(
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
) -> dict:
    """Create a Google Calendar event.

    Args:
        summary: Event title.
        start: Start time in ISO 8601 format (e.g. 2025-01-15T09:00:00-07:00).
        end: End time in ISO 8601 format.
        description: Optional event description.
        location: Optional event location.

    Returns:
        Dict with the created event's id, summary, start, end, and htmlLink.
    """
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    event_body: dict = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location

    created = service.events().insert(calendarId="primary", body=event_body).execute()

    return {
        "id": created["id"],
        "summary": created.get("summary", ""),
        "start": created["start"].get("dateTime", created["start"].get("date", "")),
        "end": created["end"].get("dateTime", created["end"].get("date", "")),
        "htmlLink": created.get("htmlLink", ""),
    }


def main() -> None:
    summary = os.environ.get("SUMMARY", "")
    start = os.environ.get("START", "")
    end = os.environ.get("END", "")
    description = os.environ.get("DESCRIPTION", "")
    location = os.environ.get("LOCATION", "")

    if not summary or not start or not end:
        print(
            json.dumps({"error": "SUMMARY, START, and END are required"}),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        event = create_event(summary, start, end, description, location)
        print(json.dumps(event, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
