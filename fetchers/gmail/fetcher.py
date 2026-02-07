#!/usr/bin/env python3
"""Gmail fetcher - retrieves emails matching a query.

Requires GOOGLE_CREDENTIALS_JSON env var containing the OAuth2 credentials
(refresh token, client ID, client secret).
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
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
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
                plain_text = base64.urlsafe_b64decode(data).decode(
                    "utf-8", errors="replace"
                )
        elif part_mime == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode(
                    "utf-8", errors="replace"
                )
                html_text = BeautifulSoup(html, "html.parser").get_text(
                    separator="\n", strip=True
                )
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
