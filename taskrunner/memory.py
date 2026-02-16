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

    def __init__(
        self,
        workspace_dir: str,
        timezone_name: str = "UTC",
        max_daily_entries: int = 50,
        max_long_term_lines: int = 500,
    ):
        self._workspace = Path(workspace_dir)
        self._memory_dir = self._workspace / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._max_daily_entries = max_daily_entries
        self._max_long_term_lines = max_long_term_lines
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

        # Rate limit: count existing entries in today's file
        if path.exists():
            entry_count = sum(
                1 for line in path.read_text().splitlines()
                if line.startswith("- [")
            )
            if entry_count >= self._max_daily_entries:
                logger.warning("Daily memory limit reached (%d entries)", self._max_daily_entries)
                return f"Daily memory limit reached ({self._max_daily_entries} entries). Try again tomorrow."

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

        # Rate limit: check line count
        line_count = len(path.read_text().splitlines())
        if line_count >= self._max_long_term_lines:
            logger.warning("Long-term memory limit reached (%d lines)", self._max_long_term_lines)
            return f"Long-term memory limit reached ({self._max_long_term_lines} lines). Consider editing existing entries."

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

    def search_memory(self, query: str, max_results: int = 20) -> str:
        """Case-insensitive substring search across all memory files.

        Searches daily files (newest first) and MEMORY.md. Only matches
        entry lines (starting with '- [') in daily files, skipping headers.

        Returns results formatted as [YYYY-MM-DD L{n}] entry text so the
        LLM can reference them in edit/delete calls.
        """
        query_lower = query.lower()
        results: list[str] = []

        # Search daily files, newest first
        daily_files = sorted(self._memory_dir.glob("*.md"), reverse=True)
        for path in daily_files:
            try:
                lines = path.read_text().splitlines()
            except OSError:
                continue
            date_str = path.stem  # e.g. "2026-01-15"
            for i, line in enumerate(lines, start=1):
                if len(results) >= max_results:
                    break
                if not line.startswith("- ["):
                    continue
                if query_lower in line.lower():
                    results.append(f"[{date_str} L{i}] {line}")
            if len(results) >= max_results:
                break

        # Search long-term memory
        lt_path = self.long_term_path
        if lt_path.exists() and len(results) < max_results:
            try:
                lines = lt_path.read_text().splitlines()
                for i, line in enumerate(lines, start=1):
                    if len(results) >= max_results:
                        break
                    if query_lower in line.lower():
                        results.append(f"[long_term L{i}] {line}")
            except OSError:
                pass

        if not results:
            return f"No memories found matching '{query}'."
        return f"Found {len(results)} result(s):\n" + "\n".join(results)

    def delete_memory(self, date_str: str, line_number: int) -> str:
        """Delete a specific memory entry by date and line number.

        Args:
            date_str: "YYYY-MM-DD" for daily files, or "long_term" for MEMORY.md.
            line_number: 1-based line number within the file.

        Returns:
            Confirmation or error message.
        """
        path = self._resolve_memory_path(date_str)
        if path is None:
            return f"Invalid date format: {date_str}. Use YYYY-MM-DD or 'long_term'."
        if not path.exists():
            return f"No memory file found for {date_str}."

        lines = path.read_text().splitlines()
        if line_number < 1 or line_number > len(lines):
            return f"Line {line_number} out of range (file has {len(lines)} lines)."

        removed = lines.pop(line_number - 1)
        path.write_text("\n".join(lines) + "\n" if lines else "")
        return f"Deleted line {line_number} from {date_str}: {removed}"

    def edit_memory(self, date_str: str, line_number: int, new_text: str) -> str:
        """Replace a specific memory entry by date and line number.

        Args:
            date_str: "YYYY-MM-DD" for daily files, or "long_term" for MEMORY.md.
            line_number: 1-based line number within the file.
            new_text: Replacement text for the line.

        Returns:
            Confirmation with old/new content, or error message.
        """
        path = self._resolve_memory_path(date_str)
        if path is None:
            return f"Invalid date format: {date_str}. Use YYYY-MM-DD or 'long_term'."
        if not path.exists():
            return f"No memory file found for {date_str}."

        lines = path.read_text().splitlines()
        if line_number < 1 or line_number > len(lines):
            return f"Line {line_number} out of range (file has {len(lines)} lines)."

        old_text = lines[line_number - 1]
        lines[line_number - 1] = new_text
        path.write_text("\n".join(lines) + "\n")
        return f"Edited line {line_number} in {date_str}:\n  old: {old_text}\n  new: {new_text}"

    def list_memory_files(self) -> str:
        """List all memory files with entry counts and sizes.

        Returns:
            Formatted list of daily files (newest first) and MEMORY.md.
        """
        lines: list[str] = []

        # Daily files, newest first
        daily_files = sorted(self._memory_dir.glob("*.md"), reverse=True)
        for path in daily_files:
            try:
                content = path.read_text()
                entry_count = sum(1 for line in content.splitlines() if line.startswith("- ["))
                size = path.stat().st_size
                lines.append(f"  {path.stem}  {entry_count} entries  {size} bytes")
            except OSError:
                lines.append(f"  {path.stem}  (unreadable)")

        # Long-term memory
        lt_path = self.long_term_path
        if lt_path.exists():
            try:
                content = lt_path.read_text()
                line_count = len(content.splitlines())
                size = lt_path.stat().st_size
                lines.append(f"  MEMORY.md  {line_count} lines  {size} bytes")
            except OSError:
                lines.append("  MEMORY.md  (unreadable)")

        if not lines:
            return "No memory files found."
        return "Memory files:\n" + "\n".join(lines)

    def compact_daily_files(self, days_to_keep: int = 7) -> str:
        """Remove daily files older than days_to_keep.

        For files with entries: appends a summary line to MEMORY.md, then deletes.
        For empty files (header only): just deletes.

        Args:
            days_to_keep: Number of recent days to preserve.

        Returns:
            Summary of compaction results.
        """
        today = datetime.now(self._tz).date()
        cutoff = today - timedelta(days=days_to_keep)
        compacted = 0

        daily_files = sorted(self._memory_dir.glob("*.md"))
        for path in daily_files:
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_date >= cutoff:
                continue

            # Count entries
            try:
                content = path.read_text()
            except OSError:
                continue
            entry_count = sum(1 for line in content.splitlines() if line.startswith("- ["))

            if entry_count > 0:
                # Append summary to MEMORY.md
                lt_path = self.long_term_path
                if not lt_path.exists():
                    lt_path.write_text("# Long-Term Memory\n\n")
                with open(lt_path, "a") as f:
                    f.write(f"- [{file_date.isoformat()}] {entry_count} entries (compacted)\n")

            path.unlink()
            compacted += 1
            logger.info("Compacted memory file: %s (%d entries)", path.name, entry_count)

        if compacted == 0:
            return "No files to compact."
        return f"Compacted {compacted} file(s) older than {days_to_keep} days."

    def _resolve_memory_path(self, date_str: str) -> Path | None:
        """Resolve a date string to a memory file path.

        Args:
            date_str: "YYYY-MM-DD" or "long_term".

        Returns:
            Path to the file, or None if date_str is invalid.
        """
        if date_str == "long_term":
            return self.long_term_path
        try:
            d = date.fromisoformat(date_str)
            return self.daily_path(d)
        except ValueError:
            return None
