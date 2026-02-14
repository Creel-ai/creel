#!/usr/bin/env python3
"""Apple Reminders executor - interacts with Reminders.app via AppleScript.

No authentication required. Uses local osascript commands.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _run_applescript(script: str) -> str:
    """Execute an AppleScript and return its stdout."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def list_reminders(list_name: str = "Reminders", show_completed: bool = False) -> list[dict]:
    """List reminders from a list."""
    completed_filter = "" if show_completed else "whose completed is false"
    script = f'''
        tell application "Reminders"
            set reminderList to {{}}
            set theList to list "{list_name}"
            set theReminders to (every reminder of theList {completed_filter})
            repeat with theReminder in theReminders
                set dueStr to ""
                try
                    set dueStr to due date of theReminder as string
                end try
                set reminderInfo to (name of theReminder) & "|||" & (completed of theReminder as string) & "|||" & dueStr
                set end of reminderList to reminderInfo
            end repeat
            set AppleScript's text item delimiters to "\\n"
            return reminderList as text
        end tell
    '''
    output = _run_applescript(script)
    if not output:
        return []

    reminders = []
    for line in output.split("\n"):
        parts = line.split("|||")
        if len(parts) >= 3:
            reminders.append({
                "name": parts[0].strip(),
                "completed": parts[1].strip().lower() == "true",
                "due_date": parts[2].strip() or None,
            })
    return reminders


def create_reminder(
    title: str,
    due_date: str | None = None,
    list_name: str = "Reminders",
    notes: str | None = None,
) -> dict:
    """Create a new reminder."""
    safe_title = title.replace('"', '\\"')
    properties = f'name:"{safe_title}"'

    if notes:
        safe_notes = notes.replace('"', '\\"')
        properties += f', body:"{safe_notes}"'

    due_clause = ""
    if due_date:
        safe_due = due_date.replace('"', '\\"')
        due_clause = f'''
            set due date of theReminder to date "{safe_due}"
        '''

    script = f'''
        tell application "Reminders"
            set theList to list "{list_name}"
            set theReminder to make new reminder at theList with properties {{{properties}}}
            {due_clause}
            return (name of theReminder) & "|||" & (id of theReminder)
        end tell
    '''
    output = _run_applescript(script)
    parts = output.split("|||")
    return {
        "name": parts[0].strip() if parts else title,
        "id": parts[1].strip() if len(parts) > 1 else "",
        "status": "created",
    }


def complete_reminder(name: str, list_name: str = "Reminders") -> dict:
    """Mark a reminder as completed."""
    safe_name = name.replace('"', '\\"')
    script = f'''
        tell application "Reminders"
            set theList to list "{list_name}"
            set theReminder to first reminder of theList whose name is "{safe_name}"
            set completed of theReminder to true
            return name of theReminder
        end tell
    '''
    result_name = _run_applescript(script)
    return {
        "name": result_name,
        "status": "completed",
    }


def get_lists() -> list[dict]:
    """Get available reminder lists."""
    script = '''
        tell application "Reminders"
            set listInfo to {}
            repeat with theList in every list
                set info to (name of theList) & "|||" & (id of theList) & "|||" & (count of reminders of theList whose completed is false)
                set end of listInfo to info
            end repeat
            set AppleScript's text item delimiters to "\\n"
            return listInfo as text
        end tell
    '''
    output = _run_applescript(script)
    if not output:
        return []

    lists = []
    for line in output.split("\n"):
        parts = line.split("|||")
        if len(parts) >= 3:
            lists.append({
                "name": parts[0].strip(),
                "id": parts[1].strip(),
                "active_count": int(parts[2].strip()) if parts[2].strip().isdigit() else 0,
            })
    return lists


def main() -> None:
    action = os.environ.get("ACTION", "list_reminders")
    try:
        if action == "list_reminders":
            list_name = os.environ.get("LIST_NAME", "Reminders")
            show_completed = os.environ.get("SHOW_COMPLETED", "false").lower() in ("true", "1", "yes")
            result = list_reminders(list_name, show_completed)
        elif action == "create_reminder":
            title = os.environ.get("TITLE", "")
            if not title:
                print(json.dumps({"error": "TITLE is required"}), file=sys.stderr)
                sys.exit(1)
            due_date = os.environ.get("DUE_DATE") or None
            list_name = os.environ.get("LIST_NAME", "Reminders")
            notes = os.environ.get("NOTES") or None
            result = create_reminder(title, due_date, list_name, notes)
        elif action == "complete_reminder":
            name = os.environ.get("NAME", "")
            if not name:
                print(json.dumps({"error": "NAME is required"}), file=sys.stderr)
                sys.exit(1)
            list_name = os.environ.get("LIST_NAME", "Reminders")
            result = complete_reminder(name, list_name)
        elif action == "get_lists":
            result = get_lists()
        else:
            print(json.dumps({"error": f"Unknown action: {action}"}), file=sys.stderr)
            sys.exit(1)

        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
