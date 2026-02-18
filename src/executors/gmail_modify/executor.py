#!/usr/bin/env python3
"""Gmail modify executor - modify, trash, or delete messages via the Gmail API.

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
    from google_creds import get_credentials


def modify_message(
    message_id: str,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict:
    """Modify labels on a Gmail message.

    Args:
        message_id: The Gmail message ID.
        add_labels: Label IDs to add (e.g. ["INBOX", "STARRED"]).
        remove_labels: Label IDs to remove (e.g. ["UNREAD"]).

    Returns:
        Dict with the message id and updated labelIds.
    """
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    body: dict[str, list[str]] = {}
    if add_labels:
        body["addLabelIds"] = add_labels
    if remove_labels:
        body["removeLabelIds"] = remove_labels

    result = (
        service.users()
        .messages()
        .modify(userId="me", id=message_id, body=body)
        .execute()
    )

    return {
        "id": result["id"],
        "labelIds": result.get("labelIds", []),
    }


def trash_message(message_id: str) -> dict:
    """Move a Gmail message to trash.

    Args:
        message_id: The Gmail message ID.

    Returns:
        Dict with the message id.
    """
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    result = (
        service.users()
        .messages()
        .trash(userId="me", id=message_id)
        .execute()
    )

    return {
        "id": result["id"],
        "labelIds": result.get("labelIds", []),
    }


def delete_message(message_id: str) -> dict:
    """Permanently delete a Gmail message (not recoverable).

    Args:
        message_id: The Gmail message ID.

    Returns:
        Dict confirming deletion.
    """
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    (
        service.users()
        .messages()
        .delete(userId="me", id=message_id)
        .execute()
    )

    return {"id": message_id, "deleted": True}


def main() -> None:
    action = os.environ.get("ACTION", "").lower()
    message_id = os.environ.get("MESSAGE_ID", "")

    if not action:
        print(
            json.dumps({"error": "ACTION is required (modify, trash, delete)"}),
            file=sys.stderr,
        )
        sys.exit(1)

    if not message_id:
        print(
            json.dumps({"error": "MESSAGE_ID is required"}),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if action == "modify":
            add_raw = os.environ.get("ADD_LABELS", "")
            remove_raw = os.environ.get("REMOVE_LABELS", "")
            add_labels = [l.strip() for l in add_raw.split(",") if l.strip()] or None
            remove_labels = (
                [l.strip() for l in remove_raw.split(",") if l.strip()] or None
            )
            result = modify_message(message_id, add_labels, remove_labels)
        elif action == "trash":
            result = trash_message(message_id)
        elif action == "delete":
            result = delete_message(message_id)
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
