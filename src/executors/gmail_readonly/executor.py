#!/usr/bin/env python3
"""Gmail executor - retrieves emails matching a query.

Requires GOOGLE_ACCESS_TOKEN env var containing a short-lived OAuth2 access token.
Outputs JSON to stdout.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import sys

from bs4 import BeautifulSoup
from googleapiclient.discovery import build

try:
    from executors.google_creds import get_credentials
except ModuleNotFoundError:
    from google_creds import get_credentials  # type: ignore[no-redef]


def register_skill():
    """Register the gmail_readonly skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="gmail_readonly",
        label="Gmail (read-only)",
        tools=(
            ToolSpec(
                name="check_email",
                description="Search and list emails matching a Gmail query",
                params=(
                    Param(
                        name="query",
                        type="string",
                        description="Gmail search query string",
                        required=True,
                    ),
                    Param(
                        name="max_results",
                        type="string",
                        description="Maximum number of messages to return",
                    ),
                    Param(
                        name="full_body",
                        type="string",
                        description="Whether to fetch full message body (true/false)",
                    ),
                ),
            ),
            ToolSpec(
                name="read_email",
                description="Read a single email by message ID with full body",
                params=(
                    Param(
                        name="message_id",
                        type="string",
                        description="Gmail message ID",
                        required=True,
                    ),
                ),
                fixed_args={"_action": "read_single"},
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        message_id = config.args.get("message_id", "")
        if message_id:
            result = read_email(message_id)
            return json.dumps(result, indent=2)

        query = config.args.get("query", "is:unread newer_than:1d")
        max_results = int(config.args.get("max_results", 20))
        full_body = str(config.args.get("full_body", "false")).lower() in (
            "true",
            "1",
            "yes",
        )
        result = fetch_emails(query, max_results, full_body)
        return json.dumps(result, indent=2)

    return meta, execute


def fetch_emails(
    query: str = "is:unread newer_than:1d",
    max_results: int = 20,
    full_body: bool = False,
) -> list[dict]:
    """Fetch emails matching a Gmail search query.

    Args:
        query: Gmail search query string.
        max_results: Maximum number of messages to return.
        full_body: If True, fetch and decode full message body.

    Returns:
        List of email dicts with subject, from, to, date, snippet, labels,
        and optionally body.
    """
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    results = (
        service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    )

    messages = results.get("messages", [])
    emails = []

    for msg_ref in messages:
        if full_body:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="full")
                .execute()
            )
        else:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_ref["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date", "To"],
                )
                .execute()
            )

        headers = msg.get("payload", {}).get("headers", [])
        email_data = {
            "id": msg_ref["id"],
            "subject": _extract_headers(headers, ["Subject"]).get("Subject", ""),
            "from": _extract_headers(headers, ["From"]).get("From", ""),
            "to": _extract_headers(headers, ["To"]).get("To", ""),
            "date": _extract_headers(headers, ["Date"]).get("Date", ""),
            "snippet": _clean_snippet(msg.get("snippet", "")),
            "labels": msg.get("labelIds", []),
        }

        if full_body:
            email_data["body"] = _extract_body(msg.get("payload", {}))

        emails.append(email_data)

    return emails


def read_email(message_id: str) -> dict:
    """Fetch a single email by its message ID with full body.

    Args:
        message_id: Gmail message ID.

    Returns:
        Dict with id, subject, from, to, date, snippet, labels, body.
    """
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()

    headers = msg.get("payload", {}).get("headers", [])
    extracted = _extract_headers(headers, ["Subject", "From", "To", "Date"])

    return {
        "id": msg["id"],
        "subject": extracted.get("Subject", ""),
        "from": extracted.get("From", ""),
        "to": extracted.get("To", ""),
        "date": extracted.get("Date", ""),
        "snippet": _clean_snippet(msg.get("snippet", "")),
        "labels": msg.get("labelIds", []),
        "body": _extract_body(msg.get("payload", {})),
    }


def _extract_body(payload: dict) -> str:
    """Recursively extract the text body from a Gmail message payload.

    Prefers text/plain over text/html. Falls back to HTML stripping
    via BeautifulSoup if only HTML is available. Ignores binary attachments.
    """
    mime_type = payload.get("mimeType", "")

    # Simple single-part message
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
        return ""

    # Multipart message — recurse into parts
    parts = payload.get("parts", [])
    if not parts:
        return ""

    # For multipart/alternative, prefer text/plain over text/html
    plain_text = ""
    html_text = ""

    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                plain_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        elif part_mime == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                html_text = BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
        elif part_mime.startswith("multipart/"):
            # Nested multipart — recurse
            result = _extract_body(part)
            if result:
                return result

    if plain_text:
        return plain_text
    if html_text:
        return html_text

    return ""


_ZERO_WIDTH_RE = re.compile(r"[\u034f\u200b\u200c\u200d\u2060\ufeff]+")


def _clean_snippet(text: str) -> str:
    """Decode HTML entities and strip zero-width characters from a Gmail snippet."""
    text = html.unescape(text)
    text = _ZERO_WIDTH_RE.sub("", text)
    return text.strip()


def _extract_headers(headers: list[dict], keys: list[str]) -> dict[str, str]:
    """Extract specific headers from Gmail's header list.

    Header name matching is case-insensitive.
    """
    keys_lower = {k.lower(): k for k in keys}
    result: dict[str, str] = {}
    for header in headers:
        name_lower = header.get("name", "").lower()
        if name_lower in keys_lower:
            result[keys_lower[name_lower]] = header.get("value", "")
    return result


def main() -> None:
    message_id = os.environ.get("MESSAGE_ID", "")

    if message_id:
        # Single message read mode
        try:
            email = read_email(message_id)
            print(json.dumps(email, indent=2))
        except Exception as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            sys.exit(1)
        return

    query = os.environ.get("QUERY", "is:unread newer_than:1d")
    max_results = int(os.environ.get("MAX_RESULTS", "20"))
    full_body = os.environ.get("FULL_BODY", "false").lower() in ("true", "1", "yes")

    # CLI arg overrides
    if len(sys.argv) > 1:
        query = sys.argv[1]
    if len(sys.argv) > 2:
        max_results = int(sys.argv[2])
    if len(sys.argv) > 3:
        full_body = sys.argv[3].lower() in ("true", "1", "yes")

    try:
        emails = fetch_emails(query, max_results, full_body)
        print(json.dumps(emails, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
