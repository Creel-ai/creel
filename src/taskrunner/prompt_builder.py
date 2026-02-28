"""System prompt builder - assembles the system prompt from multiple sources.

Multi-file workspace injection pattern where AGENTS.md,
SOUL.md, USER.md, IDENTITY.md, and memory files are loaded and injected into
the system prompt at the start of each session.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Max characters to inject per workspace file
DEFAULT_MAX_CHARS_PER_FILE = 20_000

# Standard workspace files to inject, in order
WORKSPACE_FILES = [
    "IDENTITY.md",
    "SOUL.md",
    "USER.md",
    "AGENTS.md",
    "TOOLS.md",
]


def build_system_prompt(
    *,
    base_prompt: str | None = None,
    workspace_dir: str | None = None,
    timezone_name: str = "UTC",
    tools_config: dict | None = None,
    memory_context: str | None = None,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
) -> str:
    """Build a complete system prompt from multiple sources.

    Assemble the system prompt from:
    - A base prompt template
    - Workspace personality/context files (SOUL.md, USER.md, etc.)
    - Current date/time with timezone
    - Memory context (daily + long-term)
    - Tool descriptions

    Args:
        base_prompt: Core system prompt text. If None, uses a sensible default.
        workspace_dir: Path to workspace directory containing .md files.
        timezone_name: IANA timezone name (e.g. "America/Denver").
        tools_config: Tool configurations (for generating usage guidance).
        memory_context: Pre-formatted memory context string to inject.
        max_chars_per_file: Max chars to include per workspace file.

    Returns:
        Complete system prompt string.
    """
    sections: list[str] = []

    # 1. Base prompt
    if base_prompt:
        sections.append(base_prompt)
    else:
        sections.append("You are a personal assistant. Be concise and helpful.")

    # 2. Current date/time
    sections.append(_build_datetime_section(timezone_name))

    # 3. Workspace files (personality, identity, user context)
    if workspace_dir:
        ws_section = _build_workspace_section(workspace_dir, max_chars_per_file)
        if ws_section:
            sections.append(ws_section)

    # 4. Memory context
    if memory_context:
        sections.append(f"## Relevant Memory\n{memory_context}")

    # 5. Tool usage guidance
    if tools_config:
        tool_section = _build_tool_guidance(tools_config)
        if tool_section:
            sections.append(tool_section)

    return "\n\n".join(sections)


def _build_datetime_section(timezone_name: str) -> str:
    """Build the current date/time section."""
    try:
        tz: tzinfo = ZoneInfo(timezone_name)
    except (KeyError, ValueError):
        logger.warning("Invalid timezone %r, falling back to UTC", timezone_name)
        tz = UTC

    now = datetime.now(tz)
    formatted = now.strftime("%A, %B %d, %Y %I:%M %p %Z")
    return f"## Current Date & Time\n{formatted}\nTimezone: {timezone_name}"


def _build_workspace_section(workspace_dir: str, max_chars: int) -> str | None:
    """Load and format workspace files for injection."""
    ws_path = Path(workspace_dir)
    if not ws_path.is_dir():
        logger.warning("Workspace directory not found: %s", workspace_dir)
        return None

    parts: list[str] = []
    for filename in WORKSPACE_FILES:
        filepath = ws_path / filename
        if not filepath.exists():
            continue
        try:
            content = filepath.read_text().strip()
            if not content:
                continue
            if len(content) > max_chars:
                content = content[:max_chars] + "\n\n[... truncated]"
                logger.info("Truncated %s to %d chars", filename, max_chars)
            parts.append(f"### {filename}\n{content}")
        except OSError:
            logger.warning("Failed to read workspace file: %s", filepath)

    if not parts:
        return None

    return "## Workspace Context\n" + "\n\n".join(parts)


def _build_tool_guidance(tools_config: dict) -> str | None:
    """Build tool usage guidance section from tool configs."""
    if not tools_config:
        return None

    lines = ["## Available Tools"]
    for name, cfg in tools_config.items():
        desc = getattr(cfg, "description", str(cfg))
        lines.append(f"- **{name}**: {desc}")

    return "\n".join(lines)
