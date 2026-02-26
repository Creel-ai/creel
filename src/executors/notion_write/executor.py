#!/usr/bin/env python3
"""Notion write executor - create, update, and delete pages.

Requires NOTION_API_KEY or NOTION_TOKEN environment variable.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from executors.notion.executor import (
    _extract_title,
    _notion_request,
    _parse_json_arg,
    _validate_notion_id,
)

WRITE_ACTIONS = {
    "create_page",
    "update_page",
    "append_blocks",
    "delete_page",
}


def _summarize_write_result(action: str, data: dict[str, Any]) -> dict[str, Any]:
    """Return a concise confirmation instead of the full API response."""
    summary: dict[str, Any] = {"ok": True, "action": action}

    if action == "append_blocks":
        results = data.get("results", [])
        summary["blocks_added"] = len(results)
        if results:
            summary["parent_id"] = (
                results[0].get("parent", {}).get("page_id", "")
                or results[0].get("parent", {}).get("block_id", "")
            )
        return summary

    # create_page, update_page, delete_page all return a page object
    summary["id"] = data.get("id", "")
    summary["url"] = data.get("url", "")
    title = _extract_title(data)
    if title:
        summary["title"] = title
    if action == "delete_page":
        summary["archived"] = True
    return summary


def create_page(
    database_id: str,
    properties_json: str = "",
    children_json: str = "",
) -> dict[str, Any]:
    if not database_id:
        raise ValueError("database_id is required for action='create_page'")
    _validate_notion_id(database_id, "database_id")

    properties = _parse_json_arg(properties_json, "properties_json", dict)
    if not properties:
        raise ValueError("properties_json is required for action='create_page'")

    payload: dict[str, Any] = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }

    children = _parse_json_arg(children_json, "children_json", list)
    if children:
        payload["children"] = children

    data = _notion_request("POST", "/pages", payload=payload)
    return _summarize_write_result("create_page", data)


def update_page(
    page_id: str,
    properties_json: str = "",
) -> dict[str, Any]:
    if not page_id:
        raise ValueError("page_id is required for action='update_page'")
    _validate_notion_id(page_id, "page_id")

    properties = _parse_json_arg(properties_json, "properties_json", dict)
    if not properties:
        raise ValueError("properties_json is required for action='update_page'")

    data = _notion_request(
        "PATCH",
        f"/pages/{page_id}",
        payload={"properties": properties},
    )
    return _summarize_write_result("update_page", data)


def append_blocks(
    page_id: str,
    children_json: str = "",
) -> dict[str, Any]:
    if not page_id:
        raise ValueError("page_id is required for action='append_blocks'")
    _validate_notion_id(page_id, "page_id")

    children = _parse_json_arg(children_json, "children_json", list)
    if not children:
        raise ValueError("children_json is required for action='append_blocks'")

    data = _notion_request(
        "PATCH",
        f"/blocks/{page_id}/children",
        payload={"children": children},
    )
    return _summarize_write_result("append_blocks", data)


def delete_page(page_id: str) -> dict[str, Any]:
    if not page_id:
        raise ValueError("page_id is required for action='delete_page'")
    _validate_notion_id(page_id, "page_id")

    data = _notion_request(
        "PATCH",
        f"/pages/{page_id}",
        payload={"archived": True},
    )
    return _summarize_write_result("delete_page", data)


def run_action(
    action: str,
    *,
    page_id: str = "",
    database_id: str = "",
    properties_json: str = "",
    children_json: str = "",
) -> dict[str, Any]:
    action = action.strip()
    if action not in WRITE_ACTIONS:
        allowed = ", ".join(sorted(WRITE_ACTIONS))
        raise ValueError(f"Unknown action '{action}'. Allowed actions: {allowed}")

    if action == "create_page":
        return create_page(
            database_id=database_id,
            properties_json=properties_json,
            children_json=children_json,
        )

    if action == "update_page":
        return update_page(
            page_id=page_id,
            properties_json=properties_json,
        )

    if action == "append_blocks":
        return append_blocks(
            page_id=page_id,
            children_json=children_json,
        )

    return delete_page(page_id=page_id)


def main() -> None:
    action = os.environ.get("ACTION", "")

    if not action and len(sys.argv) > 1:
        action = sys.argv[1]

    try:
        result = run_action(
            action=action,
            page_id=os.environ.get("PAGE_ID", ""),
            database_id=os.environ.get("DATABASE_ID", ""),
            properties_json=os.environ.get("PROPERTIES_JSON", ""),
            children_json=os.environ.get("CHILDREN_JSON", ""),
        )
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
