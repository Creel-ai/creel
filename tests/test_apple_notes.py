"""Tests for Apple Notes executor — AppleScript command generation and parsing."""

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


# --- list_notes ---


@patch("executors.apple_notes.executor.subprocess.run")
def test_list_notes_parses_output(mock_run):
    """list_notes should parse AppleScript output into dicts."""
    mock_run.return_value = _mock_subprocess(
        stdout="Shopping List|||x-coredata://123|||Jan 1, 2024\nMeeting Notes|||x-coredata://456|||Jan 2, 2024"
    )

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
def test_list_notes_passes_folder_and_limit(mock_run):
    """list_notes should include folder and limit in the AppleScript."""
    mock_run.return_value = _mock_subprocess(stdout="")
    list_notes("Work", 10)
    script = mock_run.call_args[0][0][2]  # osascript -e '<script>'
    assert '"Work"' in script
    assert "10" in script


# --- search_notes ---


@patch("executors.apple_notes.executor.subprocess.run")
def test_search_notes_parses_results(mock_run):
    """search_notes should parse matching notes."""
    mock_run.return_value = _mock_subprocess(
        stdout="Recipe Ideas|||x-coredata://789"
    )
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
def test_search_notes_escapes_quotes(mock_run):
    """search_notes should escape double quotes in query."""
    mock_run.return_value = _mock_subprocess(stdout="")
    search_notes('test "quoted"')
    script = mock_run.call_args[0][0][2]
    assert 'test \\"quoted\\"' in script


# --- read_note ---


@patch("executors.apple_notes.executor.subprocess.run")
def test_read_note_parses_content(mock_run):
    """read_note should parse note name, id, modified, and body."""
    mock_run.return_value = _mock_subprocess(
        stdout="My Note|||x-coredata://123|||Jan 1, 2024|||This is the note body."
    )
    result = read_note("My Note")
    assert result["name"] == "My Note"
    assert result["id"] == "x-coredata://123"
    assert result["body"] == "This is the note body."


@patch("executors.apple_notes.executor.subprocess.run")
def test_read_note_body_with_delimiters(mock_run):
    """read_note body can contain ||| since we split with maxsplit=3."""
    mock_run.return_value = _mock_subprocess(
        stdout="Note|||id|||date|||body with ||| delimiters"
    )
    result = read_note("Note")
    assert result["body"] == "body with ||| delimiters"


@patch("executors.apple_notes.executor.subprocess.run")
def test_read_note_applescript_error(mock_run):
    """read_note should raise on AppleScript error."""
    mock_run.return_value = _mock_subprocess(returncode=1, stderr="Note not found")
    with pytest.raises(RuntimeError, match="AppleScript error"):
        read_note("Missing Note")


# --- create_note ---


@patch("executors.apple_notes.executor.subprocess.run")
def test_create_note_returns_result(mock_run):
    """create_note should return name, id, and status."""
    mock_run.return_value = _mock_subprocess(
        stdout="New Note|||x-coredata://999"
    )
    result = create_note("New Note", "Some body text")
    assert result["name"] == "New Note"
    assert result["id"] == "x-coredata://999"
    assert result["status"] == "created"


@patch("executors.apple_notes.executor.subprocess.run")
def test_create_note_includes_folder(mock_run):
    """create_note should include the target folder in the script."""
    mock_run.return_value = _mock_subprocess(stdout="Note|||id")
    create_note("Test", "Body", folder="Work")
    script = mock_run.call_args[0][0][2]
    assert '"Work"' in script


@patch("executors.apple_notes.executor.subprocess.run")
def test_create_note_escapes_content(mock_run):
    """create_note should escape double quotes in title and body."""
    mock_run.return_value = _mock_subprocess(stdout="Test|||id")
    create_note('A "title"', 'A "body"')
    script = mock_run.call_args[0][0][2]
    assert 'A \\"title\\"' in script
    assert 'A \\"body\\"' in script
