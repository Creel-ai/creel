#!/usr/bin/env python3
"""Google Sheets executor - read and write spreadsheet data via the Sheets API v4.

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


def read_sheet(spreadsheet_id: str, range: str) -> dict:
    """Read cell values from a spreadsheet range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        range: A1 notation range (e.g. "Sheet1!A1:B2").

    Returns:
        Dict with range and values (2D array).
    """
    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range)
        .execute()
    )

    return {
        "range": result.get("range", ""),
        "values": result.get("values", []),
    }


def create_spreadsheet(title: str, sheet_name: str = "", data: str = "") -> dict:
    """Create a new spreadsheet.

    Args:
        title: Spreadsheet title.
        sheet_name: Optional name for the first sheet.
        data: Optional JSON string of 2D array for initial data.

    Returns:
        Dict with spreadsheet id and url.
    """
    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    body: dict = {"properties": {"title": title}}
    if sheet_name:
        body["sheets"] = [{"properties": {"title": sheet_name}}]

    result = service.spreadsheets().create(body=body).execute()

    spreadsheet_id = result["spreadsheetId"]
    url = result.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

    # Write initial data if provided
    if data:
        rows = json.loads(data)
        actual_sheet_name = sheet_name or result["sheets"][0]["properties"]["title"]
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{actual_sheet_name}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()

    return {
        "spreadsheetId": spreadsheet_id,
        "url": url,
    }


def write_to_sheet(
    spreadsheet_id: str,
    range: str,
    data: str,
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Write values to a spreadsheet range.

    Args:
        spreadsheet_id: The spreadsheet ID.
        range: A1 notation range (e.g. "Sheet1!A1:B2").
        data: JSON string of 2D array of values.
        value_input_option: How to interpret input (USER_ENTERED or RAW).

    Returns:
        Dict with updated range and cell counts.
    """
    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    rows = json.loads(data)
    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption=value_input_option,
            body={"values": rows},
        )
        .execute()
    )

    return {
        "updatedRange": result.get("updatedRange", ""),
        "updatedRows": result.get("updatedRows", 0),
        "updatedColumns": result.get("updatedColumns", 0),
        "updatedCells": result.get("updatedCells", 0),
    }


def append_to_sheet(
    spreadsheet_id: str,
    range: str,
    data: str,
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Append rows after the last row with data in a sheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        range: A1 notation range to search for data (e.g. "Sheet1!A:B").
        data: JSON string of 2D array of rows to append.
        value_input_option: How to interpret input (USER_ENTERED or RAW).

    Returns:
        Dict with updated range and cell counts.
    """
    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    rows = json.loads(data)
    result = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption=value_input_option,
            body={"values": rows},
        )
        .execute()
    )

    updates = result.get("updates", {})
    return {
        "updatedRange": updates.get("updatedRange", ""),
        "updatedRows": updates.get("updatedRows", 0),
        "updatedColumns": updates.get("updatedColumns", 0),
        "updatedCells": updates.get("updatedCells", 0),
    }


def main() -> None:
    action = os.environ.get("ACTION", "").lower()

    if not action:
        print(
            json.dumps({"error": "ACTION is required (read, create, write, append)"}),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if action == "read":
            spreadsheet_id = os.environ.get("SPREADSHEET_ID", "")
            range_ = os.environ.get("RANGE", "")
            if not spreadsheet_id or not range_:
                raise ValueError("SPREADSHEET_ID and RANGE are required for read")
            result = read_sheet(spreadsheet_id, range_)
        elif action == "create":
            title = os.environ.get("TITLE", "")
            if not title:
                raise ValueError("TITLE is required for create")
            sheet_name = os.environ.get("SHEET_NAME", "")
            data = os.environ.get("DATA", "")
            result = create_spreadsheet(title, sheet_name, data)
        elif action == "write":
            spreadsheet_id = os.environ.get("SPREADSHEET_ID", "")
            range_ = os.environ.get("RANGE", "")
            data = os.environ.get("DATA", "")
            if not spreadsheet_id or not range_ or not data:
                raise ValueError("SPREADSHEET_ID, RANGE, and DATA are required for write")
            value_input_option = os.environ.get("VALUE_INPUT_OPTION", "USER_ENTERED")
            result = write_to_sheet(spreadsheet_id, range_, data, value_input_option)
        elif action == "append":
            spreadsheet_id = os.environ.get("SPREADSHEET_ID", "")
            range_ = os.environ.get("RANGE", "")
            data = os.environ.get("DATA", "")
            if not spreadsheet_id or not range_ or not data:
                raise ValueError("SPREADSHEET_ID, RANGE, and DATA are required for append")
            value_input_option = os.environ.get("VALUE_INPUT_OPTION", "USER_ENTERED")
            result = append_to_sheet(spreadsheet_id, range_, data, value_input_option)
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
