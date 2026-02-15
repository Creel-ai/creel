#!/usr/bin/env python3
"""Apple Notes executor - interacts with Notes.app via JXA.

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


def list_notes(folder: str = "Notes", limit: int = 25) -> list[dict]:
    """List note titles and IDs from a folder."""
    script = f'''
        const app = Application("Notes");
        const folder = app.folders.byName({json.dumps(folder)});
        const notes = folder.notes();
        const limit = Math.min(notes.length, {limit});
        const results = [];
        for (let i = 0; i < limit; i++) {{
            results.push({{
                name: notes[i].name(),
                id: notes[i].id(),
                modified: notes[i].modificationDate().toISOString()
            }});
        }}
        JSON.stringify(results);
    '''
    output = _run_jxa(script)
    if not output:
        return []
    return json.loads(output)


def search_notes(query: str) -> list[dict]:
    """Search notes by text content."""
    script = f'''
        const app = Application("Notes");
        const notes = app.notes();
        const query = {json.dumps(query)}.toLowerCase();
        const results = [];
        for (let i = 0; i < notes.length && results.length < 25; i++) {{
            try {{
                const name = notes[i].name();
                const body = notes[i].plaintext();
                if (name.toLowerCase().includes(query) || body.toLowerCase().includes(query)) {{
                    results.push({{name: name, id: notes[i].id()}});
                }}
            }} catch(e) {{}}
        }}
        JSON.stringify(results);
    '''
    output = _run_jxa(script)
    if not output:
        return []
    return json.loads(output)


def read_note(name: str) -> dict:
    """Read the full content of a note by title."""
    script = f'''
        const app = Application("Notes");
        const notes = app.notes.whose({{name: {json.dumps(name)}}})();
        if (notes.length === 0) throw new Error("Note not found: " + {json.dumps(name)});
        const n = notes[0];
        JSON.stringify({{
            name: n.name(),
            id: n.id(),
            modified: n.modificationDate().toISOString(),
            body: n.plaintext()
        }});
    '''
    output = _run_jxa(script)
    return json.loads(output)


def create_note(title: str, body: str, folder: str = "Notes") -> dict:
    """Create a new note in Apple Notes."""
    script = f'''
        const app = Application("Notes");
        const folder = app.folders.byName({json.dumps(folder)});
        const n = app.Note({{name: {json.dumps(title)}, body: {json.dumps(body)}}});
        folder.notes.push(n);
        JSON.stringify({{name: n.name(), id: n.id(), status: "created"}});
    '''
    output = _run_jxa(script)
    return json.loads(output)


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
