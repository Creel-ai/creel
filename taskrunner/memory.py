"""Memory system - file-based memory inspired by OpenClaw's workspace memory.

OpenClaw uses two memory layers:
- memory/YYYY-MM-DD.md — daily append-only logs
- MEMORY.md — curated long-term memory

This module provides the same pattern for Creel, plus a `remember` tool
that the LLM can call to persist information.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class MemoryManager:
    """Manages file-based memory in a workspace directory."""

    def __init__(self, workspace_dir: str, timezone_name: str = "UTC"):
        self._workspace = Path(workspace_dir)
        self._memory_dir = self._workspace / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._tz = ZoneInfo(timezone_name)
        except (KeyError, ValueError):
            self._tz = timezone.utc

    @property
    def long_term_path(self) -> Path:
        return self._workspace / "MEMORY.md"

    def daily_path(self, d: date | None = None) -> Path:
        if d is None:
            d = datetime.now(self._tz).date()
        return self._memory_dir / f"{d.isoformat()}.md"

    def remember(self, text: str, category: str = "general") -> str:
        """Append a memory entry to today's daily file.

        This is the implementation behind the `remember` tool.

        Args:
            text: The information to remember.
            category: Optional category tag.

        Returns:
            Confirmation message.
        """
        today = datetime.now(self._tz)
        path = self.daily_path(today.date())

        timestamp = today.strftime("%H:%M")
        entry = f"- [{timestamp}] **{category}**: {text}\n"

        # Create file with header if new
        if not path.exists():
            path.write_text(f"# Memory — {today.date().isoformat()}\n\n")

        with open(path, "a") as f:
            f.write(entry)

        logger.info("Saved memory to %s: %s", path.name, text[:80])
        return f"Remembered: {text[:100]}{'...' if len(text) > 100 else ''}"

    def update_long_term(self, text: str) -> str:
        """Append to the long-term MEMORY.md file.

        Args:
            text: Content to append.

        Returns:
            Confirmation message.
        """
        path = self.long_term_path
        if not path.exists():
            path.write_text("# Long-Term Memory\n\n")

        with open(path, "a") as f:
            f.write(f"\n{text}\n")

        logger.info("Updated long-term memory: %s", text[:80])
        return "Long-term memory updated."

    def get_recent_context(self, days: int = 2, max_chars: int = 5000) -> str | None:
        """Load recent daily memory files for context injection.

        Mirrors OpenClaw's pattern of reading today + yesterday at session start.

        Args:
            days: Number of days to look back (default: 2 = today + yesterday).
            max_chars: Maximum total characters to return.

        Returns:
            Formatted memory context string, or None if no memories exist.
        """
        today = datetime.now(self._tz).date()
        parts: list[str] = []
        total_chars = 0

        for i in range(days):
            d = today - timedelta(days=i)
            path = self.daily_path(d)
            if not path.exists():
                continue
            try:
                content = path.read_text().strip()
                if not content:
                    continue
                if total_chars + len(content) > max_chars:
                    remaining = max_chars - total_chars
                    if remaining > 100:
                        content = content[:remaining] + "\n[... truncated]"
                    else:
                        break
                parts.append(content)
                total_chars += len(content)
            except OSError:
                logger.warning("Failed to read memory file: %s", path)

        # Also include long-term memory
        lt_path = self.long_term_path
        if lt_path.exists():
            try:
                lt_content = lt_path.read_text().strip()
                if lt_content:
                    remaining = max_chars - total_chars
                    if remaining > 100:
                        if len(lt_content) > remaining:
                            lt_content = lt_content[:remaining] + "\n[... truncated]"
                        parts.append(lt_content)
            except OSError:
                logger.warning("Failed to read long-term memory")

        if not parts:
            return None

        return "\n\n---\n\n".join(parts)
