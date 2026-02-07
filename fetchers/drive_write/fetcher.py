#!/usr/bin/env python3
"""Google Drive write fetcher - uploads a file to Drive.

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
from googleapiclient.http import MediaInMemoryUpload


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


def upload_file(
    name: str,
    content: str,
    mime_type: str = "text/plain",
    folder_id: str = "",
) -> dict:
    """Upload a file to Google Drive.

    Args:
        name: File name in Drive.
        content: File content as a string.
        mime_type: MIME type of the file (default: text/plain).
        folder_id: Optional Drive folder ID to upload into.

    Returns:
        Dict with the created file's id, name, and mimeType.
    """
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    file_metadata: dict = {"name": name}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    media = MediaInMemoryUpload(
        content.encode("utf-8"),
        mimetype=mime_type,
        resumable=False,
    )

    created = (
        service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id, name, mimeType",
        )
        .execute()
    )

    return {
        "id": created["id"],
        "name": created.get("name", ""),
        "mimeType": created.get("mimeType", ""),
    }


def main() -> None:
    name = os.environ.get("NAME", "")
    content = os.environ.get("CONTENT", "")
    mime_type = os.environ.get("MIME_TYPE", "text/plain")
    folder_id = os.environ.get("FOLDER_ID", "")

    if not name or not content:
        print(
            json.dumps({"error": "NAME and CONTENT are required"}),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = upload_file(name, content, mime_type, folder_id)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
