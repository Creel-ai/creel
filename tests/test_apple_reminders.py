"""Tests for Apple Reminders executor — JXA command generation and JSON parsing."""

from __future__ import annotations

import json
import subprocess
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


def _jxa_result(data):
    """Return a mock subprocess result with JSON-encoded data."""
    return _mock_subprocess(stdout=json.dumps(data))


# --- list_reminders ---


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_parses_output(mock_run):
    """list_reminders should return parsed JSON results."""
    mock_run.return_value = _jxa_result([
        {"name": "Buy groceries", "completed": False, "due_date": "2024-02-15T15:00:00.000Z"},
        {"name": "Call dentist", "completed": False, "due_date": None},
    ])
    result = list_reminders("Reminders")
    assert len(result) == 2
    assert result[0]["name"] == "Buy groceries"
    assert result[0]["completed"] is False
    assert result[0]["due_date"] == "2024-02-15T15:00:00.000Z"
    assert result[1]["due_date"] is None


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_empty(mock_run):
    """list_reminders returns empty list when no reminders."""
    mock_run.return_value = _mock_subprocess(stdout="")
    result = list_reminders()
    assert result == []


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_uses_jxa(mock_run):
    """list_reminders should invoke osascript with -l JavaScript."""
    mock_run.return_value = _jxa_result([])
    list_reminders("Reminders")
    args = mock_run.call_args[0][0]
    assert args[0] == "osascript"
    assert args[1] == "-l"
    assert args[2] == "JavaScript"


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_filters_completed(mock_run):
    """list_reminders without show_completed should filter in JXA."""
    mock_run.return_value = _jxa_result([])
    list_reminders("Reminders", show_completed=False)
    script = mock_run.call_args[0][0][4]
    assert "!completed[i]" in script


@patch("executors.apple_reminders.executor.subprocess.run")
def test_list_reminders_show_completed(mock_run):
    """list_reminders with show_completed=True should not filter."""
    mock_run.return_value = _jxa_result([])
    list_reminders("Reminders", show_completed=True)
    script = mock_run.call_args[0][0][4]
    # Filter should be "true" (include all)
    assert "!completed[i]" not in script


# --- create_reminder ---


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_basic(mock_run):
    """create_reminder should return name, id, and status."""
    mock_run.return_value = _jxa_result({
        "name": "Buy milk", "id": "x-apple-reminder://abc", "status": "created",
    })
    result = create_reminder("Buy milk")
    assert result["name"] == "Buy milk"
    assert result["status"] == "created"


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_with_due_date(mock_run):
    """create_reminder should include JS Date parsing for due dates."""
    mock_run.return_value = _jxa_result({"name": "Task", "id": "id", "status": "created"})
    create_reminder("Task", due_date="2024-03-15T09:00:00")
    script = mock_run.call_args[0][0][4]
    assert "new Date" in script
    assert "dueDate" in script
    assert "2024-03-15T09:00:00" in script


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_with_notes(mock_run):
    """create_reminder should include notes in properties."""
    mock_run.return_value = _jxa_result({"name": "Task", "id": "id", "status": "created"})
    create_reminder("Task", notes="Extra info")
    script = mock_run.call_args[0][0][4]
    assert "Extra info" in script


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_custom_list(mock_run):
    """create_reminder should use the specified list."""
    mock_run.return_value = _jxa_result({"name": "Task", "id": "id", "status": "created"})
    create_reminder("Task", list_name="Shopping")
    script = mock_run.call_args[0][0][4]
    assert '"Shopping"' in script


@patch("executors.apple_reminders.executor.subprocess.run")
def test_create_reminder_no_due_date(mock_run):
    """create_reminder without due_date should not include Date parsing."""
    mock_run.return_value = _jxa_result({"name": "Task", "id": "id", "status": "created"})
    create_reminder("Task")
    script = mock_run.call_args[0][0][4]
    assert "new Date" not in script


# --- complete_reminder ---


@patch("executors.apple_reminders.executor.subprocess.run")
def test_complete_reminder(mock_run):
    """complete_reminder should return completed status."""
    mock_run.return_value = _jxa_result({"name": "Buy milk", "status": "completed"})
    result = complete_reminder("Buy milk")
    assert result["name"] == "Buy milk"
    assert result["status"] == "completed"


@patch("executors.apple_reminders.executor.subprocess.run")
def test_complete_reminder_jxa_error(mock_run):
    """complete_reminder should raise on JXA error."""
    mock_run.return_value = _mock_subprocess(returncode=1, stderr="Reminder not found")
    with pytest.raises(RuntimeError, match="JXA error"):
        complete_reminder("Missing Reminder")


@patch("executors.apple_reminders.executor.subprocess.run")
def test_complete_reminder_timeout(mock_run):
    """complete_reminder should raise a clear error on timeout."""
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="osascript", timeout=30)
    with pytest.raises(RuntimeError, match="timed out"):
        complete_reminder("Slow Reminder")


# --- get_lists ---


@patch("executors.apple_reminders.executor.subprocess.run")
def test_get_lists_parses_output(mock_run):
    """get_lists should return parsed JSON results."""
    mock_run.return_value = _jxa_result([
        {"name": "Reminders", "id": "id-1"},
        {"name": "Shopping", "id": "id-2"},
    ])
    result = get_lists()
    assert len(result) == 2
    assert result[0]["name"] == "Reminders"
    assert result[1]["name"] == "Shopping"


@patch("executors.apple_reminders.executor.subprocess.run")
def test_get_lists_empty(mock_run):
    """get_lists returns empty list when no lists."""
    mock_run.return_value = _mock_subprocess(stdout="")
    result = get_lists()
    assert result == []
