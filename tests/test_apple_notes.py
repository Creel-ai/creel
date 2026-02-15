"""Tests for Apple Notes executor — JXA command generation and JSON parsing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from executors.apple_notes.executor import (
    create_note,
    list_notes,
    read_note,
    search_notes,
)


def _mock_subprocess(stdout="", returncode=0, stderr=""):
    """Create a mock subprocess.run result."""
    mock = MagicMock()
    mock.stdout = stdout
    mock.stderr = stderr
    mock.returncode = returncode
    return mock


def _jxa_result(data):
    """Return a mock subprocess result with JSON-encoded data."""
    return _mock_subprocess(stdout=json.dumps(data))


# --- list_notes ---


@patch("executors.apple_notes.executor.subprocess.run")
def test_list_notes_parses_output(mock_run):
    """list_notes should return parsed JSON results."""
    mock_run.return_value = _jxa_result([
        {"name": "Shopping List", "id": "x-coredata://123", "modified": "2024-01-01T00:00:00.000Z"},
        {"name": "Meeting Notes", "id": "x-coredata://456", "modified": "2024-01-02T00:00:00.000Z"},
    ])

    result = list_notes("Notes", 25)

    assert len(result) == 2
    assert result[0]["name"] == "Shopping List"
    assert result[0]["id"] == "x-coredata://123"
    assert result[1]["name"] == "Meeting Notes"
    mock_run.assert_called_once()


@patch("executors.apple_notes.executor.subprocess.run")
def test_list_notes_empty(mock_run):
    """list_notes returns empty list when no notes."""
    mock_run.return_value = _mock_subprocess(stdout="")
    result = list_notes()
    assert result == []


@patch("executors.apple_notes.executor.subprocess.run")
def test_list_notes_uses_jxa(mock_run):
    """list_notes should invoke osascript with -l JavaScript."""
    mock_run.return_value = _jxa_result([])
    list_notes("Work", 10)
    args = mock_run.call_args[0][0]
    assert args[0] == "osascript"
    assert args[1] == "-l"
    assert args[2] == "JavaScript"


@patch("executors.apple_notes.executor.subprocess.run")
def test_list_notes_passes_folder_and_limit(mock_run):
    """list_notes should include folder and limit in the JXA script."""
    mock_run.return_value = _jxa_result([])
    list_notes("Work", 10)
    script = mock_run.call_args[0][0][4]  # osascript -l JavaScript -e '<script>'
    assert '"Work"' in script
    assert "10" in script


# --- search_notes ---


@patch("executors.apple_notes.executor.subprocess.run")
def test_search_notes_parses_results(mock_run):
    """search_notes should return parsed JSON results."""
    mock_run.return_value = _jxa_result([
        {"name": "Recipe Ideas", "id": "x-coredata://789"},
    ])
    result = search_notes("recipe")
    assert len(result) == 1
    assert result[0]["name"] == "Recipe Ideas"
    assert result[0]["id"] == "x-coredata://789"


@patch("executors.apple_notes.executor.subprocess.run")
def test_search_notes_empty(mock_run):
    """search_notes returns empty list when no matches."""
    mock_run.return_value = _mock_subprocess(stdout="")
    result = search_notes("nonexistent")
    assert result == []


@patch("executors.apple_notes.executor.subprocess.run")
def test_search_notes_escapes_query(mock_run):
    """search_notes should JSON-encode the query safely."""
    mock_run.return_value = _jxa_result([])
    search_notes('test "quoted"')
    script = mock_run.call_args[0][0][4]
    # json.dumps handles the escaping
    assert 'test \\"quoted\\"' in script


# --- read_note ---


@patch("executors.apple_notes.executor.subprocess.run")
def test_read_note_parses_content(mock_run):
    """read_note should return parsed JSON with name, id, modified, body."""
    mock_run.return_value = _jxa_result({
        "name": "My Note",
        "id": "x-coredata://123",
        "modified": "2024-01-01T00:00:00.000Z",
        "body": "This is the note body.",
    })
    result = read_note("My Note")
    assert result["name"] == "My Note"
    assert result["id"] == "x-coredata://123"
    assert result["body"] == "This is the note body."


@patch("executors.apple_notes.executor.subprocess.run")
def test_read_note_jxa_error(mock_run):
    """read_note should raise on JXA error."""
    mock_run.return_value = _mock_subprocess(returncode=1, stderr="Note not found")
    with pytest.raises(RuntimeError, match="JXA error"):
        read_note("Missing Note")


@patch("executors.apple_notes.executor.subprocess.run")
def test_read_note_timeout(mock_run):
    """read_note should raise a clear error on timeout."""
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="osascript", timeout=30)
    with pytest.raises(RuntimeError, match="timed out"):
        read_note("Slow Note")


# --- create_note ---


@patch("executors.apple_notes.executor.subprocess.run")
def test_create_note_returns_result(mock_run):
    """create_note should return name, id, and status."""
    mock_run.return_value = _jxa_result({
        "name": "New Note",
        "id": "x-coredata://999",
        "status": "created",
    })
    result = create_note("New Note", "Some body text")
    assert result["name"] == "New Note"
    assert result["id"] == "x-coredata://999"
    assert result["status"] == "created"


@patch("executors.apple_notes.executor.subprocess.run")
def test_create_note_includes_folder(mock_run):
    """create_note should include the target folder in the script."""
    mock_run.return_value = _jxa_result({"name": "Test", "id": "id", "status": "created"})
    create_note("Test", "Body", folder="Work")
    script = mock_run.call_args[0][0][4]
    assert '"Work"' in script


@patch("executors.apple_notes.executor.subprocess.run")
def test_create_note_json_encodes_content(mock_run):
    """create_note should use json.dumps for safe encoding of title and body."""
    mock_run.return_value = _jxa_result({"name": "Test", "id": "id", "status": "created"})
    create_note('A "title"', 'A "body"')
    script = mock_run.call_args[0][0][4]
    # json.dumps produces escaped quotes
    assert 'A \\"title\\"' in script
    assert 'A \\"body\\"' in script


import subprocess  # noqa: E402 — needed for TimeoutExpired in test
