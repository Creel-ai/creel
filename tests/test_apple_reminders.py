"""Tests for Apple Reminders executor — AppleScript command generation and parsing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from executors.apple_reminders.executor import (
    complete_reminder,
    create_reminder,
    get_lists,
    list_reminders,
)


def _mock_subprocess(stdout="", returncode=0, stderr=""):
    """Create a mock subprocess.run result."""
    mock = MagicMock()
    mock.stdout = stdout
    mock.stderr = stderr
    mock.returncode = returncode
    return mock


# --- list_reminders ---


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_parses_output(mock_run):
    """list_reminders should parse AppleScript output into dicts."""
    mock_run.return_value = _mock_subprocess(
        stdout="Buy groceries|||false|||Feb 15, 2024 9:00 AM\nCall dentist|||false|||"
    )
    result = list_reminders("Reminders")
    assert len(result) == 2
    assert result[0]["name"] == "Buy groceries"
    assert result[0]["completed"] is False
    assert result[0]["due_date"] == "Feb 15, 2024 9:00 AM"
    assert result[1]["due_date"] is None


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_empty(mock_run):
    """list_reminders returns empty list when no reminders."""
    mock_run.return_value = _mock_subprocess(stdout="")
    result = list_reminders()
    assert result == []


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_filters_completed(mock_run):
    """list_reminders without show_completed should filter in AppleScript."""
    mock_run.return_value = _mock_subprocess(stdout="")
    list_reminders("Reminders", show_completed=False)
    script = mock_run.call_args[0][0][2]
    assert "whose completed is false" in script


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_show_completed(mock_run):
    """list_reminders with show_completed=True should not filter."""
    mock_run.return_value = _mock_subprocess(stdout="")
    list_reminders("Reminders", show_completed=True)
    script = mock_run.call_args[0][0][2]
    assert "whose completed is false" not in script


# --- create_reminder ---


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_basic(mock_run):
    """create_reminder should return name, id, and status."""
    mock_run.return_value = _mock_subprocess(stdout="Buy milk|||x-apple-reminder://abc")
    result = create_reminder("Buy milk")
    assert result["name"] == "Buy milk"
    assert result["status"] == "created"


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_with_due_date(mock_run):
    """create_reminder should build date from components when ISO provided."""
    mock_run.return_value = _mock_subprocess(stdout="Task|||id")
    create_reminder("Task", due_date="2024-03-15T09:00:00")
    script = mock_run.call_args[0][0][2]
    assert "due date of theReminder to dueDate" in script
    assert "set year of dueDate to 2024" in script
    assert "set month of dueDate to 3" in script
    assert "set day of dueDate to 15" in script
    assert "set hours of dueDate to 9" in script


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_with_notes(mock_run):
    """create_reminder should include notes in properties."""
    mock_run.return_value = _mock_subprocess(stdout="Task|||id")
    create_reminder("Task", notes="Extra info")
    script = mock_run.call_args[0][0][2]
    assert "Extra info" in script


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_custom_list(mock_run):
    """create_reminder should use the specified list via tell block."""
    mock_run.return_value = _mock_subprocess(stdout="Task|||id")
    create_reminder("Task", list_name="Shopping")
    script = mock_run.call_args[0][0][2]
    assert '"Shopping"' in script


# --- complete_reminder ---


@patch("executors.apple_reminders.executor.subprocess.run")
def test_complete_reminder(mock_run):
    """complete_reminder should return completed status."""
    mock_run.return_value = _mock_subprocess(stdout="Buy milk")
    result = complete_reminder("Buy milk")
    assert result["name"] == "Buy milk"
    assert result["status"] == "completed"


@patch("executors.apple_reminders.executor.subprocess.run")
def test_complete_reminder_applescript_error(mock_run):
    """complete_reminder should raise on AppleScript error."""
    mock_run.return_value = _mock_subprocess(returncode=1, stderr="Reminder not found")
    with pytest.raises(RuntimeError, match="AppleScript error"):
        complete_reminder("Missing Reminder")


# --- get_lists ---


@patch("executors.apple_reminders.executor.subprocess.run")
def test_get_lists_parses_output(mock_run):
    """get_lists should parse list names, ids, and counts."""
    mock_run.return_value = _mock_subprocess(
        stdout="Reminders|||x-apple-list://1|||5\nShopping|||x-apple-list://2|||3"
    )
    result = get_lists()
    assert len(result) == 2
    assert result[0]["name"] == "Reminders"
    assert result[0]["active_count"] == 5
    assert result[1]["name"] == "Shopping"


@patch("executors.apple_reminders.executor.subprocess.run")
def test_get_lists_empty(mock_run):
    """get_lists returns empty list when no lists."""
    mock_run.return_value = _mock_subprocess(stdout="")
    result = get_lists()
    assert result == []
