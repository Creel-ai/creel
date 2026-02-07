"""Tests for secrets management."""

from __future__ import annotations

import pytest

from taskrunner.secrets import _parse_env


def test_parse_env_basic() -> None:
    content = "KEY=value\nANOTHER=thing"
    result = _parse_env(content)
    assert result == {"KEY": "value", "ANOTHER": "thing"}


def test_parse_env_quoted_values() -> None:
    content = 'KEY="quoted value"\nSINGLE=\'single quoted\''
    result = _parse_env(content)
    assert result == {"KEY": "quoted value", "SINGLE": "single quoted"}


def test_parse_env_comments_and_blanks() -> None:
    content = "# comment\n\nKEY=value\n  # another comment\nKEY2=val2"
    result = _parse_env(content)
    assert result == {"KEY": "value", "KEY2": "val2"}


def test_parse_env_equals_in_value() -> None:
    content = "KEY=value=with=equals"
    result = _parse_env(content)
    assert result == {"KEY": "value=with=equals"}


def test_parse_env_empty() -> None:
    assert _parse_env("") == {}
    assert _parse_env("# just a comment") == {}


def test_parse_env_whitespace_handling() -> None:
    content = "  KEY  =  value  "
    result = _parse_env(content)
    assert result == {"KEY": "value"}


def test_parse_env_json_escaped_double_quotes() -> None:
    """Double-quoted values with JSON escapes (as produced by setup-google-oauth.py)."""
    import json

    inner = json.dumps({"refresh_token": "tok", "client_id": "cid", "client_secret": "cs"})
    content = f"GOOGLE_CREDENTIALS_JSON={json.dumps(inner)}"
    result = _parse_env(content)
    assert json.loads(result["GOOGLE_CREDENTIALS_JSON"]) == {
        "refresh_token": "tok",
        "client_id": "cid",
        "client_secret": "cs",
    }
