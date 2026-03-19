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
    from google_creds import get_credentials  # type: ignore[no-redef]


def register_skill():
    """Register the gmail_send skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="gmail_send",
        label="Gmail (send)",
        tools=(
            ToolSpec(
                name="send_email",
                description="Send an email via Gmail",
                params=(
                    Param(
                        name="to",
                        type="string",
                        description="Recipient email address",
                        required=True,
                    ),
                    Param(
                        name="subject",
                        type="string",
                        description="Email subject line",
                        required=True,
                    ),
                    Param(
                        name="body",
                        type="string",
                        description="Plain-text email body",
                        required=True,
                    ),
                ),
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        to = config.args.get("to", "")
        subject = config.args.get("subject", "")
        body = config.args.get("body", "")
        result = send_email(to, subject, body)
        return json.dumps(result, indent=2)

    return meta, execute


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
