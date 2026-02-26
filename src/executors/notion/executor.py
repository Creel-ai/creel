#!/usr/bin/env python3
"""Notion executor - read-only access to pages and databases.

Requires NOTION_API_KEY or NOTION_TOKEN environment variable.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import requests

NOTION_API_URL = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2022-06-28"

_NOTION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _validate_notion_id(value: str, name: str) -> str:
    if not _NOTION_ID_RE.match(value):
        raise ValueError(f"{name} must be a valid Notion UUID")
    return value


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

READ_ONLY_ACTIONS = {
    "search",
    "retrieve_page",
    "query_database",
}


def _notion_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("NOTION_API_KEY") or os.environ.get("NOTION_TOKEN", "")
    if not api_key:
        raise RuntimeError("NOTION_API_KEY or NOTION_TOKEN must be set")

    notion_version = os.environ.get("NOTION_VERSION", DEFAULT_NOTION_VERSION)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": notion_version,
        "Content-Type": "application/json",
    }

    resp = requests.request(
        method,
        f"{NOTION_API_URL}{path}",
        headers=headers,
        json=payload,
        timeout=20,
    )

    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        message = ""
        try:
            data = resp.json()
            message = data.get("message", "")
        except ValueError:
            message = resp.text.strip()
        detail = message or str(e)
        raise RuntimeError(f"Notion API request failed ({resp.status_code}): {detail}") from e

    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError("Notion API returned invalid JSON") from e

    if not isinstance(data, dict):
        raise RuntimeError("Notion API returned a non-object response")

    return data


def _parse_page_size(raw: str | int | None) -> int:
    if raw in (None, ""):
        return DEFAULT_PAGE_SIZE
    try:
        size = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("page_size must be an integer") from e
    return max(1, min(size, MAX_PAGE_SIZE))


def _parse_json_arg(raw: str, arg_name: str, expected_type: type) -> Any | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{arg_name} must be valid JSON: {e.msg}") from e

    if not isinstance(parsed, expected_type):
        expected = "object" if expected_type is dict else "array"
        raise ValueError(f"{arg_name} must decode to a JSON {expected}")
    return parsed


def _plain_text(rich_text: list[dict]) -> str:
    return "".join(item.get("plain_text", "") for item in rich_text if isinstance(item, dict))


def _extract_title(obj: dict[str, Any]) -> str:
    if obj.get("object") == "database":
        title = obj.get("title", [])
        if isinstance(title, list):
            return _plain_text(title)

    properties = obj.get("properties", {})
    if not isinstance(properties, dict):
        return ""

    for prop in properties.values():
        if not isinstance(prop, dict):
            continue
        if prop.get("type") == "title":
            title = prop.get("title", [])
            if isinstance(title, list):
                text = _plain_text(title)
                if text:
                    return text

    return ""


def _summarize_property(prop: dict[str, Any]) -> Any:
    prop_type = prop.get("type", "")
    value = prop.get(prop_type)

    if prop_type in {"title", "rich_text"} and isinstance(value, list):
        return _plain_text(value)
    if prop_type in {"number", "checkbox", "url", "email", "phone_number"}:
        return value
    if prop_type == "date" and isinstance(value, dict):
        return {
            "start": value.get("start"),
            "end": value.get("end"),
        }
    if prop_type in {"select", "status"} and isinstance(value, dict):
        return value.get("name")
    if prop_type == "multi_select" and isinstance(value, list):
        return [item.get("name", "") for item in value if isinstance(item, dict)]
    if prop_type == "people" and isinstance(value, list):
        people = []
        for item in value:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name:
                people.append(name)
            else:
                people.append(item.get("id", ""))
        return people
    if prop_type == "relation" and isinstance(value, list):
        return [item.get("id", "") for item in value if isinstance(item, dict)]

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _summarize_properties(properties: dict[str, Any]) -> dict[str, Any]:
    summarized: dict[str, Any] = {}
    for name, prop in properties.items():
        if isinstance(prop, dict):
            summarized[name] = _summarize_property(prop)
    return summarized


def _summarize_result(item: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "id": item.get("id", ""),
        "object": item.get("object", ""),
        "url": item.get("url", ""),
        "title": _extract_title(item),
        "created_time": item.get("created_time", ""),
        "last_edited_time": item.get("last_edited_time", ""),
        "archived": bool(item.get("archived", False)),
        "in_trash": bool(item.get("in_trash", False)),
    }

    properties = item.get("properties", {})
    if isinstance(properties, dict):
        summary["properties"] = _summarize_properties(properties)

    return summary


def search(
    query: str = "", page_size: int = DEFAULT_PAGE_SIZE, start_cursor: str = ""
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "page_size": page_size,
    }
    if query:
        payload["query"] = query
    if start_cursor:
        payload["start_cursor"] = start_cursor

    data = _notion_request("POST", "/search", payload=payload)
    results = data.get("results", [])

    return {
        "results": [_summarize_result(item) for item in results if isinstance(item, dict)],
        "next_cursor": data.get("next_cursor"),
        "has_more": bool(data.get("has_more", False)),
    }


def retrieve_page(page_id: str) -> dict[str, Any]:
    if not page_id:
        raise ValueError("page_id is required for action='retrieve_page'")
    _validate_notion_id(page_id, "page_id")

    data = _notion_request("GET", f"/pages/{page_id}")
    return _summarize_result(data)


def query_database(
    database_id: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    start_cursor: str = "",
    filter_obj: dict[str, Any] | None = None,
    sorts_obj: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not database_id:
        raise ValueError("database_id is required for action='query_database'")
    _validate_notion_id(database_id, "database_id")

    payload: dict[str, Any] = {
        "page_size": page_size,
    }
    if start_cursor:
        payload["start_cursor"] = start_cursor
    if filter_obj is not None:
        payload["filter"] = filter_obj
    if sorts_obj is not None:
        payload["sorts"] = sorts_obj

    data = _notion_request("POST", f"/databases/{database_id}/query", payload=payload)
    results = data.get("results", [])

    return {
        "results": [_summarize_result(item) for item in results if isinstance(item, dict)],
        "next_cursor": data.get("next_cursor"),
        "has_more": bool(data.get("has_more", False)),
    }


def run_action(
    action: str,
    *,
    query: str = "",
    page_id: str = "",
    database_id: str = "",
    filter_json: str = "",
    sorts_json: str = "",
    page_size: str | int | None = None,
    start_cursor: str = "",
) -> dict[str, Any]:
    action = action.strip()
    if action not in READ_ONLY_ACTIONS:
        allowed = ", ".join(sorted(READ_ONLY_ACTIONS))
        raise ValueError(f"Unknown action '{action}'. Allowed actions: {allowed}")

    parsed_page_size = _parse_page_size(page_size)

    if action == "search":
        return search(query=query, page_size=parsed_page_size, start_cursor=start_cursor)

    if action == "retrieve_page":
        return retrieve_page(page_id=page_id)

    filter_obj = _parse_json_arg(filter_json, "filter_json", dict)
    sorts_obj = _parse_json_arg(sorts_json, "sorts_json", list)

    return query_database(
        database_id=database_id,
        page_size=parsed_page_size,
        start_cursor=start_cursor,
        filter_obj=filter_obj,
        sorts_obj=sorts_obj,
    )


def main() -> None:
    action = os.environ.get("ACTION", "")

    # Optional CLI override for quick manual testing
    if not action and len(sys.argv) > 1:
        action = sys.argv[1]

    try:
        result = run_action(
            action=action,
            query=os.environ.get("QUERY", ""),
            page_id=os.environ.get("PAGE_ID", ""),
            database_id=os.environ.get("DATABASE_ID", ""),
            filter_json=os.environ.get("FILTER_JSON", ""),
            sorts_json=os.environ.get("SORTS_JSON", ""),
            page_size=os.environ.get("PAGE_SIZE", ""),
            start_cursor=os.environ.get("START_CURSOR", ""),
        )
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
