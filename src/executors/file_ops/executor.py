#!/usr/bin/env python3
"""File operations executor - read, write, edit, and list files.

All paths are sandboxed to a workspace root (WORKSPACE env var, default /workspace).
Symlinks are resolved before access checks to prevent traversal.
Outputs JSON to stdout on success, stderr on error.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys


def register_skill():
    """Register the file_ops skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="file_ops",
        label="File Operations",
        tools=(
            ToolSpec(
                name="read_file",
                description="Read a file from the workspace",
                params=(
                    Param(
                        name="file_path",
                        type="string",
                        description="Path to the file to read",
                        required=True,
                    ),
                    Param(
                        name="offset",
                        type="string",
                        description="Line number to start reading from",
                    ),
                    Param(
                        name="limit",
                        type="string",
                        description="Number of lines to read",
                    ),
                ),
                fixed_args={"action": "read"},
            ),
            ToolSpec(
                name="write_file",
                description="Write content to a file in the workspace",
                params=(
                    Param(
                        name="file_path",
                        type="string",
                        description="Path to the file to write",
                        required=True,
                    ),
                    Param(
                        name="content",
                        type="string",
                        description="Content to write to the file",
                        required=True,
                    ),
                ),
                fixed_args={"action": "write"},
            ),
            ToolSpec(
                name="edit_file",
                description="Replace text in a file",
                params=(
                    Param(
                        name="file_path",
                        type="string",
                        description="Path to the file to edit",
                        required=True,
                    ),
                    Param(
                        name="old_text",
                        type="string",
                        description="Text to find and replace",
                        required=True,
                    ),
                    Param(
                        name="new_text",
                        type="string",
                        description="Replacement text",
                        required=True,
                    ),
                ),
                fixed_args={"action": "edit"},
            ),
            ToolSpec(
                name="list_files",
                description="List files in a directory",
                params=(
                    Param(
                        name="directory",
                        type="string",
                        description="Directory to list (default: workspace root)",
                    ),
                    Param(
                        name="pattern",
                        type="string",
                        description="Glob pattern to filter files (default: *)",
                    ),
                    Param(
                        name="recursive",
                        type="string",
                        description="Recursively list files (default: false)",
                    ),
                ),
                fixed_args={"action": "list"},
            ),
        ),
        needs_network=False,
    )

    def execute(config: ExecutorConfig) -> str:
        from creel.orchestrator import _env_override

        action = config.args.get("action", "")
        if action not in ACTIONS:
            raise ValueError(f"file_ops: unknown action '{action}'")
        env_map = {
            "workspace": "WORKSPACE",
            "action": "ACTION",
            "file_path": "FILE_PATH",
            "content": "CONTENT",
            "old_text": "OLD_TEXT",
            "new_text": "NEW_TEXT",
            "offset": "OFFSET",
            "limit": "LIMIT",
            "directory": "DIRECTORY",
            "pattern": "PATTERN",
            "recursive": "RECURSIVE",
        }
        env_vars = {
            env_key: str(config.args[arg_key])
            for arg_key, env_key in env_map.items()
            if config.args.get(arg_key, "")
        }
        with _env_override(env_vars):
            result = ACTIONS[action]()
        return json.dumps(result, indent=2)

    return meta, execute


def _workspace() -> str:
    """Return resolved workspace root path."""
    return os.path.realpath(os.environ.get("WORKSPACE", "/workspace"))


def _safe_path(file_path: str) -> str:
    """Resolve a path and verify it stays within the workspace.

    Resolves symlinks via os.path.realpath so that symlink-based
    traversal is blocked.

    Raises ValueError if the resolved path escapes the workspace.
    """
    workspace = _workspace()
    resolved = os.path.realpath(os.path.join(workspace, file_path))
    # Ensure resolved path is workspace itself or a child of it
    if resolved != workspace and not resolved.startswith(workspace + os.sep):
        raise ValueError(f"Path escapes workspace: {file_path}")
    return resolved


def action_read() -> dict:
    """Read a file, optionally with offset/limit (line-based)."""
    file_path = os.environ.get("FILE_PATH", "")
    if not file_path:
        return {"error": "FILE_PATH is required"}

    try:
        offset = int(os.environ.get("OFFSET", "0"))
        limit = int(os.environ.get("LIMIT", "0"))
    except ValueError:
        return {"error": "OFFSET and LIMIT must be integers"}

    try:
        resolved = _safe_path(file_path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(resolved):
        return {"error": f"File not found: {file_path}"}
    if not os.path.isfile(resolved):
        return {"error": f"Not a file: {file_path}"}

    try:
        with open(resolved, encoding="utf-8") as f:
            lines = f.readlines()

        if offset > 0:
            lines = lines[offset:]
        if limit > 0:
            lines = lines[:limit]

        content = "".join(lines)
        return {"path": file_path, "content": content, "lines": len(lines)}
    except OSError as e:
        return {"error": str(e)}


def action_write() -> dict:
    """Write content to a file (creates parent dirs if needed)."""
    file_path = os.environ.get("FILE_PATH", "")
    content = os.environ.get("CONTENT", "")
    if not file_path:
        return {"error": "FILE_PATH is required"}

    try:
        resolved = _safe_path(file_path)
    except ValueError as e:
        return {"error": str(e)}

    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return {"path": file_path, "bytes_written": len(content.encode())}
    except OSError as e:
        return {"error": str(e)}


def action_edit() -> dict:
    """Replace OLD_TEXT with NEW_TEXT in a file."""
    file_path = os.environ.get("FILE_PATH", "")
    old_text = os.environ.get("OLD_TEXT", "")
    new_text = os.environ.get("NEW_TEXT", "")
    if not file_path:
        return {"error": "FILE_PATH is required"}
    if not old_text:
        return {"error": "OLD_TEXT is required"}

    try:
        resolved = _safe_path(file_path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(resolved):
        return {"error": f"File not found: {file_path}"}

    try:
        with open(resolved, encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return {"error": "OLD_TEXT not found in file"}

        updated = content.replace(old_text, new_text)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(updated)

        return {"path": file_path, "replacements": count}
    except OSError as e:
        return {"error": str(e)}


def action_list() -> dict:
    """List files in a directory, optionally filtered by glob pattern."""
    directory = os.environ.get("DIRECTORY", ".")
    pattern = os.environ.get("PATTERN", "*")
    recursive = os.environ.get("RECURSIVE", "false").lower() == "true"

    try:
        resolved = _safe_path(directory)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(resolved):
        return {"error": f"Directory not found: {directory}"}
    if not os.path.isdir(resolved):
        return {"error": f"Not a directory: {directory}"}

    try:
        entries = []
        if recursive:
            for root, dirs, files in os.walk(resolved):
                for name in sorted(dirs + files):
                    full = os.path.join(root, name)
                    if os.path.islink(full):
                        continue
                    rel = os.path.relpath(full, resolved)
                    if fnmatch.fnmatch(name, pattern):
                        entries.append(
                            {
                                "name": rel,
                                "type": "directory" if os.path.isdir(full) else "file",
                                "size": os.path.getsize(full) if os.path.isfile(full) else 0,
                            }
                        )
        else:
            for name in sorted(os.listdir(resolved)):
                full = os.path.join(resolved, name)
                if os.path.islink(full):
                    continue
                if fnmatch.fnmatch(name, pattern):
                    entries.append(
                        {
                            "name": name,
                            "type": "directory" if os.path.isdir(full) else "file",
                            "size": os.path.getsize(full) if os.path.isfile(full) else 0,
                        }
                    )

        return {"directory": directory, "entries": entries, "count": len(entries)}
    except OSError as e:
        return {"error": str(e)}


ACTIONS = {
    "read": action_read,
    "write": action_write,
    "edit": action_edit,
    "list": action_list,
}


def main() -> None:
    action = os.environ.get("ACTION", "").lower()
    if action not in ACTIONS:
        result = {"error": f"Unknown action: {action!r}. Valid: {', '.join(ACTIONS)}"}
        print(json.dumps(result), file=sys.stderr)
        sys.exit(1)

    result = ACTIONS[action]()
    if "error" in result:
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
