#!/usr/bin/env python3
"""PDF reader executor - extract text and search content in PDF files.

Accepts a file path (absolute or relative to WORKSPACE).
Uses PyMuPDF (fitz) for text extraction.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys


def register_skill():
    """Register the pdf_reader skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="pdf_reader",
        label="PDF Reader",
        tools=(
            ToolSpec(
                name="read_pdf",
                description="Extract text from a PDF file",
                params=(
                    Param(
                        name="file_path",
                        type="string",
                        description="Path to the PDF file",
                        required=True,
                    ),
                    Param(
                        name="pages",
                        type="string",
                        description=(
                            "Page range to extract: 'all' (default), single page '3', "
                            "range '1-5', or comma-separated '1,3,5-7'"
                        ),
                    ),
                ),
                fixed_args={"action": "read"},
            ),
            ToolSpec(
                name="search_pdf",
                description="Search for text within a PDF file",
                params=(
                    Param(
                        name="file_path",
                        type="string",
                        description="Path to the PDF file",
                        required=True,
                    ),
                    Param(
                        name="query",
                        type="string",
                        description="Text to search for in the PDF",
                        required=True,
                    ),
                ),
                fixed_args={"action": "search"},
            ),
        ),
        needs_network=False,
    )

    def execute(config: ExecutorConfig) -> str:
        from creel.orchestrator import _env_override

        action = config.args.get("action", "")
        if action not in ACTIONS:
            raise ValueError(f"pdf_reader: unknown action '{action}'")
        env_map = {
            "workspace": "WORKSPACE",
            "action": "ACTION",
            "file_path": "FILE_PATH",
            "pages": "PAGES",
            "query": "QUERY",
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
    if os.path.isabs(file_path):
        resolved = os.path.realpath(file_path)
    else:
        resolved = os.path.realpath(os.path.join(workspace, file_path))
    if resolved != workspace and not resolved.startswith(workspace + os.sep):
        raise ValueError(f"Path escapes workspace: {file_path}")
    return resolved


def _parse_page_ranges(pages_str: str, total_pages: int) -> list[int]:
    """Parse a page range string into a sorted list of 0-based page indices.

    Accepts: 'all', single page '3', range '1-5', comma-separated '1,3,5-7'.
    Page numbers in the input are 1-based; output is 0-based.
    """
    if not pages_str or pages_str.strip().lower() == "all":
        return list(range(total_pages))

    indices: set[int] = set()
    for part in pages_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            match = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
            if not match:
                raise ValueError(f"Invalid page range: '{part}'")
            start = int(match.group(1))
            end = int(match.group(2))
            if start < 1 or end < start:
                raise ValueError(f"Invalid page range: '{part}'")
            for p in range(start, end + 1):
                if 1 <= p <= total_pages:
                    indices.add(p - 1)
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid page number: '{part}'")
            p = int(part)
            if 1 <= p <= total_pages:
                indices.add(p - 1)

    return sorted(indices)


def _open_pdf(file_path: str):
    """Open a PDF file with PyMuPDF, handling common errors."""
    import fitz

    resolved = _safe_path(file_path)
    if not os.path.exists(resolved):
        raise FileNotFoundError(f"File not found: {file_path}")
    if not os.path.isfile(resolved):
        raise ValueError(f"Not a file: {file_path}")

    doc = fitz.open(resolved)
    if doc.is_encrypted:
        doc.close()
        raise ValueError(f"PDF is encrypted and cannot be read: {file_path}")
    return doc


def action_read() -> dict:
    """Extract text from a PDF file, optionally for specific pages."""
    file_path = os.environ.get("FILE_PATH", "")
    pages_str = os.environ.get("PAGES", "all")

    if not file_path:
        return {"error": "FILE_PATH is required"}

    try:
        doc = _open_pdf(file_path)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        return {"error": str(e)}

    try:
        page_indices = _parse_page_ranges(pages_str, len(doc))
    except ValueError as e:
        doc.close()
        return {"error": str(e)}

    total_pages = len(doc)
    pages_output = []
    for idx in page_indices:
        page = doc[idx]
        text = page.get_text()
        pages_output.append(
            {
                "page": idx + 1,
                "text": text,
            }
        )

    total_chars = sum(len(p["text"]) for p in pages_output)
    doc.close()

    return {
        "file_path": file_path,
        "total_pages": total_pages,
        "pages_read": len(pages_output),
        "total_chars": total_chars,
        "pages": pages_output,
    }


def action_search() -> dict:
    """Search for text within a PDF and return matching pages with context."""
    file_path = os.environ.get("FILE_PATH", "")
    query = os.environ.get("QUERY", "")

    if not file_path:
        return {"error": "FILE_PATH is required"}
    if not query:
        return {"error": "QUERY is required"}

    try:
        doc = _open_pdf(file_path)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        return {"error": str(e)}

    query_lower = query.lower()
    matches = []

    for idx in range(len(doc)):
        page = doc[idx]
        text = page.get_text()
        if query_lower in text.lower():
            # Extract matching passages with context
            passages = _extract_passages(text, query, context_chars=150)
            matches.append(
                {
                    "page": idx + 1,
                    "passages": passages,
                }
            )

    total_pages = len(doc)
    doc.close()

    return {
        "file_path": file_path,
        "query": query,
        "total_pages": total_pages,
        "matching_pages": len(matches),
        "matches": matches,
    }


def _extract_passages(text: str, query: str, context_chars: int = 150) -> list[str]:
    """Extract passages containing the query with surrounding context."""
    passages = []
    text_lower = text.lower()
    query_lower = query.lower()
    start = 0

    while True:
        pos = text_lower.find(query_lower, start)
        if pos == -1:
            break

        ctx_start = max(0, pos - context_chars)
        ctx_end = min(len(text), pos + len(query) + context_chars)

        passage = text[ctx_start:ctx_end].strip()
        if ctx_start > 0:
            passage = "..." + passage
        if ctx_end < len(text):
            passage = passage + "..."

        passages.append(passage)
        start = pos + len(query)

        # Limit to 10 passages per page
        if len(passages) >= 10:
            break

    return passages


ACTIONS = {
    "read": action_read,
    "search": action_search,
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
