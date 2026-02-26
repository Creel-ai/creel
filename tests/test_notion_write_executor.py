"""Tests for the Notion write executor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from executors.notion_write.executor import run_action


@pytest.fixture(autouse=True)
def _set_notion_env(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "test-notion-key")
    monkeypatch.setenv("NOTION_VERSION", "2022-06-28")


def _mock_response(payload: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


PAGE_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
DB_UUID = "12345678-abcd-ef01-2345-678901abcdef"


@patch("executors.notion.executor.requests.request")
def test_create_page(mock_request):
    mock_request.return_value = _mock_response({"id": PAGE_UUID, "object": "page"})

    result = run_action(
        "create_page",
        database_id=DB_UUID,
        properties_json='{"Name": {"title": [{"text": {"content": "New Page"}}]}}',
    )

    call = mock_request.call_args
    assert call.args[0] == "POST"
    assert call.args[1].endswith("/pages")
    payload = call.kwargs["json"]
    assert payload["parent"]["database_id"] == DB_UUID
    assert "Name" in payload["properties"]
    assert result["id"] == PAGE_UUID


@patch("executors.notion.executor.requests.request")
def test_create_page_with_children(mock_request):
    mock_request.return_value = _mock_response({"id": PAGE_UUID, "object": "page"})

    children = '[{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "Hello"}}]}}]'
    run_action(
        "create_page",
        database_id=DB_UUID,
        properties_json='{"Name": {"title": [{"text": {"content": "Page"}}]}}',
        children_json=children,
    )

    payload = mock_request.call_args.kwargs["json"]
    assert len(payload["children"]) == 1


@patch("executors.notion.executor.requests.request")
def test_update_page(mock_request):
    mock_request.return_value = _mock_response({"id": PAGE_UUID, "object": "page"})

    run_action(
        "update_page",
        page_id=PAGE_UUID,
        properties_json='{"Status": {"select": {"name": "Done"}}}',
    )

    call = mock_request.call_args
    assert call.args[0] == "PATCH"
    assert call.args[1].endswith(f"/pages/{PAGE_UUID}")
    assert "Status" in call.kwargs["json"]["properties"]


@patch("executors.notion.executor.requests.request")
def test_append_blocks(mock_request):
    mock_request.return_value = _mock_response({"results": []})

    children = '[{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "Appended"}}]}}]'
    run_action(
        "append_blocks",
        page_id=PAGE_UUID,
        children_json=children,
    )

    call = mock_request.call_args
    assert call.args[0] == "PATCH"
    assert call.args[1].endswith(f"/blocks/{PAGE_UUID}/children")
    assert len(call.kwargs["json"]["children"]) == 1


@patch("executors.notion.executor.requests.request")
def test_delete_page(mock_request):
    mock_request.return_value = _mock_response({"id": PAGE_UUID, "archived": True})

    result = run_action("delete_page", page_id=PAGE_UUID)

    call = mock_request.call_args
    assert call.args[0] == "PATCH"
    assert call.args[1].endswith(f"/pages/{PAGE_UUID}")
    assert call.kwargs["json"]["archived"] is True
    assert result["archived"] is True


def test_unknown_action_raises():
    with pytest.raises(ValueError, match="Unknown action"):
        run_action("search")


def test_create_page_requires_database_id():
    with pytest.raises(ValueError, match="database_id"):
        run_action("create_page", properties_json='{"Name": {}}')


def test_create_page_requires_properties():
    with pytest.raises(ValueError, match="properties_json"):
        run_action("create_page", database_id=DB_UUID)


def test_update_page_requires_page_id():
    with pytest.raises(ValueError, match="page_id"):
        run_action("update_page", properties_json='{"Status": {}}')


def test_update_page_requires_properties():
    with pytest.raises(ValueError, match="properties_json"):
        run_action("update_page", page_id=PAGE_UUID)


def test_append_blocks_requires_page_id():
    with pytest.raises(ValueError, match="page_id"):
        run_action("append_blocks", children_json='[{}]')


def test_append_blocks_requires_children():
    with pytest.raises(ValueError, match="children_json"):
        run_action("append_blocks", page_id=PAGE_UUID)


def test_delete_page_requires_page_id():
    with pytest.raises(ValueError, match="page_id"):
        run_action("delete_page")


def test_page_id_path_traversal_rejected():
    with pytest.raises(ValueError, match="valid Notion UUID"):
        run_action("update_page", page_id="../users", properties_json='{"a": 1}')


def test_database_id_path_traversal_rejected():
    with pytest.raises(ValueError, match="valid Notion UUID"):
        run_action("create_page", database_id="../users", properties_json='{"a": 1}')


@patch("executors.notion.executor.requests.request")
def test_http_error_surfaces_message(mock_request):
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {"message": "invalid properties"}
    response.raise_for_status.side_effect = requests.HTTPError("400 Client Error")
    mock_request.return_value = response

    with pytest.raises(RuntimeError, match="Notion API request failed"):
        run_action(
            "create_page",
            database_id=DB_UUID,
            properties_json='{"Name": {}}',
        )
