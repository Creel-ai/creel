#!/usr/bin/env python3
"""Google Docs executor - read and write document content via the Docs API v1.

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


def _extract_text(body: dict) -> str:
    """Walk structural elements to extract plain text from a Docs body."""
    parts: list[str] = []
    for element in body.get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for elem in paragraph.get("elements", []):
            text_run = elem.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts)


def read_document(document_id: str) -> dict:
    """Get document content as plain text.

    Args:
        document_id: The Google Docs document ID.

    Returns:
        Dict with document title and plain text content.
    """
    creds = get_credentials()
    service = build("docs", "v1", credentials=creds, cache_discovery=False)

    doc = service.documents().get(documentId=document_id).execute()

    return {
        "documentId": doc["documentId"],
        "title": doc.get("title", ""),
        "content": _extract_text(doc.get("body", {})),
    }


def create_document(title: str, body: str = "") -> dict:
    """Create a new document.

    Args:
        title: Document title.
        body: Optional initial body text.

    Returns:
        Dict with document id and url.
    """
    creds = get_credentials()
    service = build("docs", "v1", credentials=creds, cache_discovery=False)

    doc = service.documents().create(body={"title": title}).execute()
    document_id = doc["documentId"]

    if body:
        service.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": body,
                        }
                    }
                ]
            },
        ).execute()

    return {
        "documentId": document_id,
        "url": f"https://docs.google.com/document/d/{document_id}",
    }


def append_text(document_id: str, text: str) -> dict:
    """Append text to the end of a document.

    Args:
        document_id: The Google Docs document ID.
        text: Text to append.

    Returns:
        Dict confirming the append.
    """
    creds = get_credentials()
    service = build("docs", "v1", credentials=creds, cache_discovery=False)

    service.documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {
                    "insertText": {
                        "endOfSegmentLocation": {"segmentId": ""},
                        "text": text,
                    }
                }
            ]
        },
    ).execute()

    return {"documentId": document_id, "appended": True}


def replace_text(
    document_id: str,
    find: str,
    replace_with: str,
    match_case: bool = True,
) -> dict:
    """Find and replace text in a document.

    Args:
        document_id: The Google Docs document ID.
        find: Text to find.
        replace_with: Replacement text.
        match_case: Whether the search is case-sensitive.

    Returns:
        Dict with the number of occurrences changed.
    """
    creds = get_credentials()
    service = build("docs", "v1", credentials=creds, cache_discovery=False)

    result = (
        service.documents()
        .batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {
                                "text": find,
                                "matchCase": match_case,
                            },
                            "replaceText": replace_with,
                        }
                    }
                ]
            },
        )
        .execute()
    )

    replies = result.get("replies", [{}])
    occurrences = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)

    return {
        "documentId": document_id,
        "occurrencesChanged": occurrences,
    }


def insert_text(document_id: str, text: str, index: int) -> dict:
    """Insert text at a specific index in a document.

    Args:
        document_id: The Google Docs document ID.
        text: Text to insert.
        index: The zero-based character index to insert at.

    Returns:
        Dict confirming the insert.
    """
    creds = get_credentials()
    service = build("docs", "v1", credentials=creds, cache_discovery=False)

    service.documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {
                    "insertText": {
                        "location": {"index": index},
                        "text": text,
                    }
                }
            ]
        },
    ).execute()

    return {"documentId": document_id, "inserted": True}


def main() -> None:
    action = os.environ.get("ACTION", "").lower()

    if not action:
        print(
            json.dumps({"error": "ACTION is required (read, create, append, replace, insert)"}),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if action == "read":
            document_id = os.environ.get("DOCUMENT_ID", "")
            if not document_id:
                raise ValueError("DOCUMENT_ID is required for read")
            result = read_document(document_id)
        elif action == "create":
            title = os.environ.get("TITLE", "")
            if not title:
                raise ValueError("TITLE is required for create")
            body = os.environ.get("BODY", "")
            result = create_document(title, body)
        elif action == "append":
            document_id = os.environ.get("DOCUMENT_ID", "")
            text = os.environ.get("TEXT", "")
            if not document_id or not text:
                raise ValueError("DOCUMENT_ID and TEXT are required for append")
            result = append_text(document_id, text)
        elif action == "replace":
            document_id = os.environ.get("DOCUMENT_ID", "")
            find = os.environ.get("FIND", "")
            replace_with = os.environ.get("REPLACE_WITH", "")
            if not document_id or not find:
                raise ValueError("DOCUMENT_ID and FIND are required for replace")
            match_case = os.environ.get("MATCH_CASE", "true").lower() in (
                "true",
                "1",
                "yes",
            )
            result = replace_text(document_id, find, replace_with, match_case)
        elif action == "insert":
            document_id = os.environ.get("DOCUMENT_ID", "")
            text = os.environ.get("TEXT", "")
            index = os.environ.get("INDEX", "")
            if not document_id or not text or not index:
                raise ValueError("DOCUMENT_ID, TEXT, and INDEX are required for insert")
            result = insert_text(document_id, text, int(index))
        else:
            print(
                json.dumps({"error": f"Unknown action: {action}"}),
                file=sys.stderr,
            )
            sys.exit(1)

        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
