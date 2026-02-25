#!/usr/bin/env python3
"""Google Drive executor - lists and reads files.

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
        # If the query doesn't look like Drive query syntax (no operators),
        # wrap it as a fullText search.
        _DRIVE_OPERATORS = ("contains", "=", "!=", "<", ">", "<=", ">=", " in ", "has")
        if not any(op in query for op in _DRIVE_OPERATORS):
            escaped = query.replace("\\", "\\\\").replace("'", "\\'")
            query = f"fullText contains '{escaped}'"
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
