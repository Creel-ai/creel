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
    from google_creds import get_credentials  # type: ignore[no-redef]


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

    result = service.users().messages().modify(userId="me", id=message_id, body=body).execute()

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

    result = service.users().messages().trash(userId="me", id=message_id).execute()

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

    (service.users().messages().delete(userId="me", id=message_id).execute())

    return {"id": message_id, "deleted": True}


_MAX_BATCH_SIZE = 10  # Keep small to limit blast radius from LLM errors


def batch_modify(
    message_ids: list[str],
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict:
    """Batch modify labels on multiple Gmail messages.

    Uses the Gmail batchModify API for efficiency (up to _MAX_BATCH_SIZE messages).

    Args:
        message_ids: List of Gmail message IDs.
        add_labels: Label IDs to add.
        remove_labels: Label IDs to remove.

    Returns:
        Dict with count of modified messages.
    """
    if len(message_ids) > _MAX_BATCH_SIZE:
        raise ValueError(
            f"Too many message IDs ({len(message_ids)}); Gmail batch limit is {_MAX_BATCH_SIZE}"
        )
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    body: dict = {"ids": message_ids}
    if add_labels:
        body["addLabelIds"] = add_labels
    if remove_labels:
        body["removeLabelIds"] = remove_labels

    service.users().messages().batchModify(userId="me", body=body).execute()

    return {"modified": len(message_ids), "ids": message_ids}


def batch_trash(message_ids: list[str]) -> dict:
    """Move multiple Gmail messages to trash.

    Uses batchModify with TRASH label for efficiency.

    Args:
        message_ids: List of Gmail message IDs.

    Returns:
        Dict with count of trashed messages.
    """
    return batch_modify(message_ids, add_labels=["TRASH"], remove_labels=["INBOX"])


def batch_delete(message_ids: list[str]) -> dict:
    """Permanently delete multiple Gmail messages (not recoverable).

    Uses the Gmail batchDelete API.

    Args:
        message_ids: List of Gmail message IDs.

    Returns:
        Dict confirming deletion.
    """
    if len(message_ids) > _MAX_BATCH_SIZE:
        raise ValueError(
            f"Too many message IDs ({len(message_ids)}); Gmail batch limit is {_MAX_BATCH_SIZE}"
        )
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    service.users().messages().batchDelete(userId="me", body={"ids": message_ids}).execute()

    return {"deleted": len(message_ids), "ids": message_ids}


def _parse_ids(raw: str) -> list[str]:
    """Parse a comma-separated string of message IDs."""
    return [mid.strip() for mid in raw.split(",") if mid.strip()]


def main() -> None:
    action = os.environ.get("ACTION", "").lower()
    message_id = os.environ.get("MESSAGE_ID", "")
    message_ids_raw = os.environ.get("MESSAGE_IDS", "")

    if not action:
        print(
            json.dumps(
                {
                    "error": "ACTION is required (modify, trash, delete, batch_modify, batch_trash, batch_delete)"
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    # Batch actions use MESSAGE_IDS (comma-separated)
    is_batch = action.startswith("batch_")

    if is_batch:
        message_ids = _parse_ids(message_ids_raw) or _parse_ids(message_id)
        if not message_ids:
            print(
                json.dumps(
                    {"error": "MESSAGE_IDS is required for batch actions (comma-separated)"}
                ),
                file=sys.stderr,
            )
            sys.exit(1)
    else:
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
            add_labels = [label.strip() for label in add_raw.split(",") if label.strip()] or None
            remove_labels = [
                label.strip() for label in remove_raw.split(",") if label.strip()
            ] or None
            result = modify_message(message_id, add_labels, remove_labels)
        elif action == "trash":
            result = trash_message(message_id)
        elif action == "delete":
            result = delete_message(message_id)
        elif action == "batch_modify":
            add_raw = os.environ.get("ADD_LABELS", "")
            remove_raw = os.environ.get("REMOVE_LABELS", "")
            add_labels = [label.strip() for label in add_raw.split(",") if label.strip()] or None
            remove_labels = [
                label.strip() for label in remove_raw.split(",") if label.strip()
            ] or None
            result = batch_modify(message_ids, add_labels, remove_labels)
        elif action == "batch_trash":
            result = batch_trash(message_ids)
        elif action == "batch_delete":
            result = batch_delete(message_ids)
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
