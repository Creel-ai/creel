"""Live smoke tests for the Notion executor.

These tests require network access and a valid Notion integration token.

Environment variables:
  - NOTION_API_KEY (required for all tests; loaded from secrets if unset)
  - NOTION_TEST_PAGE_ID (optional, enables page retrieval smoke test)
  - NOTION_TEST_DATABASE_ID (optional, enables database query smoke test)
  - NOTION_SMOKE_SECRETS_FILE (optional; defaults to secrets/notion.env.enc)

Run with:
    python -m pytest tests/test_notion_smoke.py -v -m smoke --no-cov
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from executors.notion.executor import run_action
from creel.secrets import decrypt_env_file

# Mark this module as smoke-only.
pytestmark = [pytest.mark.smoke]


def _resolve_secrets_path() -> Path:
    configured = os.environ.get("NOTION_SMOKE_SECRETS_FILE", "secrets/notion.env.enc")
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[1] / path


def _load_secret_env() -> tuple[dict[str, str], str]:
    path = _resolve_secrets_path()
    if not path.exists():
        return {}, f"{path} does not exist"

    try:
        return decrypt_env_file(path), ""
    except Exception as e:  # noqa: BLE001
        return {}, str(e)


_SECRET_ENV, _SECRET_ENV_ERROR = _load_secret_env()


@pytest.fixture(autouse=True)
def _inject_secret_env(monkeypatch):
    """Populate env vars from notion secrets file when shell env is unset."""
    for key, value in _SECRET_ENV.items():
        if value and not os.environ.get(key):
            monkeypatch.setenv(key, value)


def _get_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return _SECRET_ENV.get(name, "").strip()


def _missing_reason(required_vars: list[str]) -> str:
    missing = [name for name in required_vars if not _get_env(name)]
    if not missing:
        return ""

    base = ", ".join(missing)
    if _SECRET_ENV_ERROR:
        return (
            f"missing {base}; could not use {_resolve_secrets_path()} "
            f"({_SECRET_ENV_ERROR})"
        )
    return f"missing {base}; set env vars or update {_resolve_secrets_path()}"


_KEY_SKIP_REASON = _missing_reason(["NOTION_API_KEY"])
_PAGE_SKIP_REASON = _missing_reason(["NOTION_API_KEY", "NOTION_TEST_PAGE_ID"])
_DB_SKIP_REASON = _missing_reason(["NOTION_API_KEY", "NOTION_TEST_DATABASE_ID"])

requires_notion_key = pytest.mark.skipif(bool(_KEY_SKIP_REASON), reason=_KEY_SKIP_REASON)
requires_notion_page = pytest.mark.skipif(bool(_PAGE_SKIP_REASON), reason=_PAGE_SKIP_REASON)
requires_notion_database = pytest.mark.skipif(bool(_DB_SKIP_REASON), reason=_DB_SKIP_REASON)


@requires_notion_key
def test_live_search_smoke():
    """Search should succeed with a live token and return normalized shape."""
    result = run_action(
        "search",
        query="creel-notion-smoke-query",
        page_size="3",
    )

    assert isinstance(result, dict)
    assert "results" in result
    assert "has_more" in result
    assert "next_cursor" in result
    assert isinstance(result["results"], list)
    assert len(result["results"]) <= 3


@requires_notion_page
def test_live_retrieve_page_smoke():
    """Retrieving a configured page should return a normalized page object."""
    page_id = _get_env("NOTION_TEST_PAGE_ID")
    result = run_action("retrieve_page", page_id=page_id)

    assert isinstance(result, dict)
    assert result.get("object") == "page"
    assert result.get("id")
    assert "title" in result
    assert "properties" in result


@requires_notion_database
def test_live_query_database_smoke():
    """Querying a configured database should succeed with normalized output."""
    database_id = _get_env("NOTION_TEST_DATABASE_ID")
    result = run_action(
        "query_database",
        database_id=database_id,
        page_size="3",
    )

    assert isinstance(result, dict)
    assert "results" in result
    assert "has_more" in result
    assert "next_cursor" in result
    assert isinstance(result["results"], list)
    assert len(result["results"]) <= 3
