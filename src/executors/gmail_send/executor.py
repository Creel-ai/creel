#!/usr/bin/env python3
"""Gmail send executor - sends an email via the Gmail API.

Requires GOOGLE_ACCESS_TOKEN env var containing a short-lived OAuth2 access token.
Outputs JSON to stdout.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from email.mime.text import MIMEText

from googleapiclient.discovery import build

try:
    from executors.google_creds import get_credentials
except ModuleNotFoundError:
    from google_creds import get_credentials


def send_email(to: str, subject: str, body: str) -> dict:
    """Send an email via the Gmail API.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.

    Returns:
        Dict with the sent message's id and threadId.
    """
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()

    return {
        "id": sent["id"],
        "threadId": sent.get("threadId", ""),
    }


def main() -> None:
    to = os.environ.get("TO", "")
    subject = os.environ.get("SUBJECT", "")
    body = os.environ.get("BODY", "")

    if not to or not subject:
        print(
            json.dumps({"error": "TO and SUBJECT are required"}),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = send_email(to, subject, body)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
