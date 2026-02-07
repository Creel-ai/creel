#!/usr/bin/env python3
"""Google Calendar write fetcher - creates calendar events.

Requires GOOGLE_CREDENTIALS_JSON env var containing the OAuth2 credentials
(refresh token, client ID, client secret).
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


def get_credentials() -> Credentials:
    """Build credentials from environment variable."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON environment variable not set")

    creds_data = json.loads(creds_json)
    creds = Credentials(
        token=None,
        refresh_token=creds_data["refresh_token"],
        client_id=creds_data["client_id"],
        client_secret=creds_data["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return creds


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
    service = build("calendar", "v3", credentials=creds)

    event_body: dict = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location

    created = (
        service.events()
        .insert(calendarId="primary", body=event_body)
        .execute()
    )

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
