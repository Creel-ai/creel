#!/usr/bin/env python3
"""Google Drive fetcher - lists and reads files.

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


def list_files(query: str = "", max_results: int = 20) -> list[dict]:
    """List files from Google Drive.

    Args:
        query: Drive search query (e.g. "mimeType='application/pdf'").
            See https://developers.google.com/drive/api/guides/search-files
        max_results: Maximum number of files to return.

    Returns:
        List of file dicts with id, name, mimeType, modifiedTime, and size.
    """
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    params: dict = {
        "pageSize": max_results,
        "fields": "files(id, name, mimeType, modifiedTime, size)",
    }
    if query:
        params["q"] = query

    results = service.files().list(**params).execute()

    files = []
    for f in results.get("files", []):
        files.append(
            {
                "id": f["id"],
                "name": f.get("name", ""),
                "mimeType": f.get("mimeType", ""),
                "modifiedTime": f.get("modifiedTime", ""),
                "size": f.get("size", ""),
            }
        )

    return files


def main() -> None:
    query = os.environ.get("QUERY", "")
    max_results = int(os.environ.get("MAX_RESULTS", "20"))

    # CLI arg overrides
    if len(sys.argv) > 1:
        query = sys.argv[1]
    if len(sys.argv) > 2:
        max_results = int(sys.argv[2])

    try:
        files = list_files(query, max_results)
        print(json.dumps(files, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
