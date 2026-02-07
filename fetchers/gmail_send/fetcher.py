#!/usr/bin/env python3
"""Gmail send fetcher - sends an email via the Gmail API.

Requires GOOGLE_CREDENTIALS_JSON env var containing the OAuth2 credentials
(refresh token, client ID, client secret).
Outputs JSON to stdout.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from email.mime.text import MIMEText

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
    service = build("gmail", "v1", credentials=creds)

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    sent = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw})
        .execute()
    )

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
