"""Tests for the Notion executor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from executors.notion.executor import run_action


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


@patch("executors.notion.executor.requests.request")
def test_search_sends_expected_request(mock_request):
    mock_request.return_value = _mock_response(
        {
            "results": [],
            "next_cursor": None,
            "has_more": False,
        }
    )

    run_action("search", query="weekly plan", page_size="5")

    call = mock_request.call_args
    assert call.args[0] == "POST"
    assert call.args[1].endswith("/search")
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-notion-key"
    assert call.kwargs["headers"]["Notion-Version"] == "2022-06-28"
    assert call.kwargs["json"]["query"] == "weekly plan"
    assert call.kwargs["json"]["page_size"] == 5


@patch("executors.notion.executor.requests.request")
def test_search_parses_titles(mock_request):
    mock_request.return_value = _mock_response(
        {
            "results": [
                {
                    "object": "page",
                    "id": "page-1",
                    "url": "https://notion.so/page-1",
                    "last_edited_time": "2026-01-01T00:00:00.000Z",
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": [{"plain_text": "Roadmap"}],
                        }
                    },
                },
                {
                    "object": "database",
                    "id": "db-1",
                    "url": "https://notion.so/db-1",
                    "last_edited_time": "2026-01-02T00:00:00.000Z",
                    "title": [{"plain_text": "Tasks"}],
                },
            ],
            "next_cursor": None,
            "has_more": False,
        }
    )

    result = run_action("search", query="roadmap")
    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "Roadmap"
    assert result["results"][1]["title"] == "Tasks"


@patch("executors.notion.executor.requests.request")
def test_retrieve_page_hits_page_endpoint(mock_request):
    page_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    mock_request.return_value = _mock_response(
        {
            "object": "page",
            "id": page_uuid,
            "url": f"https://notion.so/{page_uuid}",
            "properties": {},
        }
    )

    run_action("retrieve_page", page_id=page_uuid)

    call = mock_request.call_args
    assert call.args[0] == "GET"
    assert call.args[1].endswith(f"/pages/{page_uuid}")


@patch("executors.notion.executor.requests.request")
def test_query_database_sends_filter_and_sorts(mock_request):
    mock_request.return_value = _mock_response(
        {
            "results": [],
            "next_cursor": None,
            "has_more": False,
        }
    )

    db_uuid = "12345678-abcd-ef01-2345-678901abcdef"
    run_action(
        "query_database",
        database_id=db_uuid,
        filter_json='{"property":"Status","status":{"equals":"Todo"}}',
        sorts_json='[{"timestamp":"last_edited_time","direction":"descending"}]',
        page_size="500",
    )

    call = mock_request.call_args
    assert call.args[0] == "POST"
    assert call.args[1].endswith(f"/databases/{db_uuid}/query")
    assert call.kwargs["json"]["page_size"] == 100  # clamped
    assert call.kwargs["json"]["filter"]["property"] == "Status"
    assert call.kwargs["json"]["sorts"][0]["direction"] == "descending"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NOTION_API_KEY"):
        run_action("search", query="test")


def test_unknown_action_raises():
    with pytest.raises(ValueError, match="Unknown action"):
        run_action("create_page")


def test_retrieve_page_requires_page_id():
    with pytest.raises(ValueError, match="page_id"):
        run_action("retrieve_page")


def test_query_database_requires_database_id():
    with pytest.raises(ValueError, match="database_id"):
        run_action("query_database")


def test_invalid_filter_json_raises():
    with pytest.raises(ValueError, match="filter_json"):
        run_action(
            "query_database",
            database_id="12345678-abcd-ef01-2345-678901abcdef",
            filter_json="{not-json",
        )


def test_page_id_path_traversal_rejected():
    with pytest.raises(ValueError, match="valid Notion UUID"):
        run_action("retrieve_page", page_id="../users")


def test_database_id_path_traversal_rejected():
    with pytest.raises(ValueError, match="valid Notion UUID"):
        run_action("query_database", database_id="../users")


@patch("executors.notion.executor.requests.request")
def test_http_error_surface_message(mock_request):
    response = MagicMock()
    response.status_code = 401
    response.json.return_value = {"message": "unauthorized"}
    response.raise_for_status.side_effect = requests.HTTPError("401 Client Error")
    mock_request.return_value = response

    with pytest.raises(RuntimeError, match="Notion API request failed"):
        run_action("search", query="test")
