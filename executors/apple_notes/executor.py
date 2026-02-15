#!/usr/bin/env python3
"""Apple Notes executor - interacts with Notes.app via AppleScript.

No authentication required. Uses local osascript commands.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _run_applescript(script: str, timeout: int = 30) -> str:
    """Execute an AppleScript and return its stdout."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"AppleScript timed out after {timeout}s")
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def list_notes(folder: str = "Notes", limit: int = 25) -> list[dict]:
    """List note titles and IDs from a folder."""
    script = f'''
        tell application "Notes"
            set noteList to {{}}
            set theFolder to folder "{folder}"
            set noteCount to count of notes of theFolder
            if noteCount > {limit} then set noteCount to {limit}
            repeat with i from 1 to noteCount
                set theNote to note i of theFolder
                set noteInfo to (name of theNote) & "|||" & (id of theNote) & "|||" & (modification date of theNote as string)
                set end of noteList to noteInfo
            end repeat
            set AppleScript's text item delimiters to "\\n"
            return noteList as text
        end tell
    '''
    output = _run_applescript(script)
    if not output:
        return []

    notes = []
    for line in output.split("\n"):
        parts = line.split("|||")
        if len(parts) >= 3:
            notes.append({
                "name": parts[0].strip(),
                "id": parts[1].strip(),
                "modified": parts[2].strip(),
            })
    return notes


def search_notes(query: str) -> list[dict]:
    """Search notes by text content."""
    safe_query = query.replace('"', '\\"')
    script = f'''
        tell application "Notes"
            set matchingNotes to {{}}
            set allNotes to every note
            repeat with theNote in allNotes
                try
                    if (plaintext of theNote) contains "{safe_query}" or (name of theNote) contains "{safe_query}" then
                        set noteInfo to (name of theNote) & "|||" & (id of theNote)
                        set end of matchingNotes to noteInfo
                    end if
                end try
            end repeat
            set AppleScript's text item delimiters to "\\n"
            return matchingNotes as text
        end tell
    '''
    output = _run_applescript(script)
    if not output:
        return []

    results = []
    for line in output.split("\n"):
        parts = line.split("|||")
        if len(parts) >= 2:
            results.append({
                "name": parts[0].strip(),
                "id": parts[1].strip(),
            })
    return results


def read_note(name: str) -> dict:
    """Read the full content of a note by title."""
    safe_name = name.replace('"', '\\"')
    script = f'''
        tell application "Notes"
            set theNote to first note whose name is "{safe_name}"
            set noteBody to plaintext of theNote
            set noteName to name of theNote
            set noteId to id of theNote
            set noteModified to modification date of theNote as string
            return noteName & "|||" & noteId & "|||" & noteModified & "|||" & noteBody
        end tell
    '''
    output = _run_applescript(script)
    parts = output.split("|||", 3)
    if len(parts) < 4:
        return {"error": f"Note '{name}' not found or could not be read"}

    return {
        "name": parts[0].strip(),
        "id": parts[1].strip(),
        "modified": parts[2].strip(),
        "body": parts[3].strip(),
    }


def create_note(title: str, body: str, folder: str = "Notes") -> dict:
    """Create a new note in Apple Notes."""
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"').replace("\n", "\\n")
    script = f'''
        tell application "Notes"
            set theFolder to folder "{folder}"
            set theNote to make new note at theFolder with properties {{name:"{safe_title}", body:"{safe_body}"}}
            return (name of theNote) & "|||" & (id of theNote)
        end tell
    '''
    output = _run_applescript(script)
    parts = output.split("|||")
    return {
        "name": parts[0].strip() if parts else title,
        "id": parts[1].strip() if len(parts) > 1 else "",
        "status": "created",
    }


def main() -> None:
    action = os.environ.get("ACTION", "list_notes")
    try:
        if action == "list_notes":
            folder = os.environ.get("FOLDER", "Notes")
            limit = int(os.environ.get("LIMIT", "25"))
            result = list_notes(folder, limit)
        elif action == "search_notes":
            query = os.environ.get("QUERY", "")
            if not query:
                print(json.dumps({"error": "QUERY is required"}), file=sys.stderr)
                sys.exit(1)
            result = search_notes(query)
        elif action == "read_note":
            name = os.environ.get("NAME", "")
            if not name:
                print(json.dumps({"error": "NAME is required"}), file=sys.stderr)
                sys.exit(1)
            result = read_note(name)
        elif action == "create_note":
            title = os.environ.get("TITLE", "")
            body = os.environ.get("BODY", "")
            folder = os.environ.get("FOLDER", "Notes")
            if not title or not body:
                print(json.dumps({"error": "TITLE and BODY are required"}), file=sys.stderr)
                sys.exit(1)
            result = create_note(title, body, folder)
        else:
            print(json.dumps({"error": f"Unknown action: {action}"}), file=sys.stderr)
            sys.exit(1)

        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
