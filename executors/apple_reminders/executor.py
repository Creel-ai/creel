#!/usr/bin/env python3
"""Apple Reminders executor - interacts with Reminders.app via JXA.

No authentication required. Uses osascript with JavaScript for Automation
(JXA), which is significantly faster than AppleScript for bulk operations.

NOTE: This executor is host-only — osascript cannot run inside Docker.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _run_jxa(script: str, timeout: int = 30) -> str:
    """Execute a JXA script and return its stdout."""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"JXA script timed out after {timeout}s")
    if result.returncode != 0:
        raise RuntimeError(f"JXA error: {result.stderr.strip()}")
    return result.stdout.strip()


def list_reminders(list_name: str = "Reminders", show_completed: bool = False) -> list[dict]:
    """List reminders from a list.

    Uses bulk property access for names/completed status (fast), then
    fetches due dates only for the matching subset.
    """
    filter_js = "true" if show_completed else "!completed[i]"
    script = f'''
        const app = Application("Reminders");
        const list = app.lists.byName({json.dumps(list_name)});
        const names = list.reminders.name();
        const completed = list.reminders.completed();
        const indices = [];
        for (let i = 0; i < names.length && indices.length < 50; i++) {{
            if ({filter_js}) indices.push(i);
        }}
        const results = indices.map(i => {{
            let due = null;
            try {{ due = list.reminders[i].dueDate(); if (due) due = due.toISOString(); }} catch(e) {{}}
            return {{name: names[i], completed: completed[i], due_date: due}};
        }});
        JSON.stringify(results);
    '''
    output = _run_jxa(script)
    if not output:
        return []
    return json.loads(output)


def create_reminder(
    title: str,
    due_date: str | None = None,
    list_name: str = "Reminders",
    notes: str | None = None,
) -> dict:
    """Create a new reminder."""
    props = {"name": title}
    if notes:
        props["body"] = notes

    due_js = ""
    if due_date:
        due_js = f"""
        try {{
            const d = new Date({json.dumps(due_date)});
            if (!isNaN(d.getTime())) r.dueDate = d;
        }} catch(e) {{}}
        """

    script = f'''
        const app = Application("Reminders");
        const list = app.lists.byName({json.dumps(list_name)});
        const r = app.Reminder({json.dumps(props)});
        list.reminders.push(r);
        {due_js}
        JSON.stringify({{name: r.name(), id: r.id(), status: "created"}});
    '''
    output = _run_jxa(script)
    return json.loads(output)


def complete_reminder(name: str, list_name: str = "Reminders") -> dict:
    """Mark a reminder as completed by name."""
    script = f'''
        const app = Application("Reminders");
        const list = app.lists.byName({json.dumps(list_name)});
        const names = list.reminders.name();
        const completed = list.reminders.completed();
        let found = false;
        for (let i = 0; i < names.length; i++) {{
            if (names[i] === {json.dumps(name)} && !completed[i]) {{
                list.reminders[i].completed = true;
                found = true;
                break;
            }}
        }}
        if (!found) throw new Error("Reminder not found: " + {json.dumps(name)});
        JSON.stringify({{name: {json.dumps(name)}, status: "completed"}});
    '''
    output = _run_jxa(script)
    return json.loads(output)


def get_lists() -> list[dict]:
    """Get available reminder lists."""
    script = '''
        const app = Application("Reminders");
        const lists = app.lists();
        const results = lists.map(l => ({name: l.name(), id: l.id()}));
        JSON.stringify(results);
    '''
    output = _run_jxa(script)
    if not output:
        return []
    return json.loads(output)


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
