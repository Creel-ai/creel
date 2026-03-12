"""Memory system - file-based memory inspired by OpenClaw's workspace memory.

OpenClaw uses two memory layers:
- memory/YYYY-MM-DD.md — daily append-only logs
- MEMORY.md — curated long-term memory

This module provides the same pattern for Creel, plus a `remember` tool
that the LLM can call to persist information.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ENTRY_PREFIX_RE = re.compile(r"^- \[\d{2}:\d{2}\] \*\*[\w-]+\*\*:\s*")


def _strip_entry_prefix(line: str) -> str:
    """Strip the timestamp/category prefix from a daily memory entry line."""
    return _ENTRY_PREFIX_RE.sub("", line).strip()


def _recency_weight(date_str: str, today: date, half_life: float) -> float:
    """Compute temporal decay weight. Evergreen (long_term) entries get 1.0."""
    if date_str == "long_term":
        return 1.0
    try:
        age = (today - date.fromisoformat(date_str)).days
    except ValueError:
        return 1.0
    return 2.0 ** (-age / half_life)


class MemoryIndex:
    """SQLite FTS5 full-text search index for memory files.

    This is a rebuildable cache — markdown files remain the source of truth.
    """

    def __init__(self, db_path: Path, recency_half_life_days: float = 30.0):
        self._db_path = db_path
        self._half_life = recency_half_life_days
        self._available = False
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(str(db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Test FTS5 availability
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                "source, date_str, line_number, content, "
                "tokenize='porter unicode61'"
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS file_meta ("
                "path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL"
                ")"
            )
            self._conn.commit()
            self._available = True
        except (sqlite3.Error, OSError) as exc:
            logger.warning("FTS5 index unavailable: %s", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def reindex_if_needed(self, memory_dir: Path, long_term_path: Path) -> int:
        """Scan memory files and re-index any that have changed. Returns count of files re-indexed."""
        if not self._available or self._conn is None:
            return 0

        reindexed = 0
        current_paths: set[str] = set()

        # Gather all files to check (only date-named daily files)
        files_to_check: list[tuple[Path, str]] = []
        for md_path in memory_dir.glob("*.md"):
            try:
                date.fromisoformat(md_path.stem)
            except ValueError:
                logger.debug("Skipping non-date file in memory dir: %s", md_path.name)
                continue
            files_to_check.append((md_path, md_path.stem))
            current_paths.add(str(md_path))
        if long_term_path.exists():
            files_to_check.append((long_term_path, "long_term"))
            current_paths.add(str(long_term_path))

        for file_path, date_str in files_to_check:
            try:
                stat = file_path.stat()
            except OSError:
                continue
            mtime_ns = stat.st_mtime_ns
            size = stat.st_size

            row = self._conn.execute(
                "SELECT mtime_ns, size FROM file_meta WHERE path = ?",
                (str(file_path),),
            ).fetchone()
            if row and row[0] == mtime_ns and row[1] == size:
                continue

            self.reindex_file(file_path, date_str, _commit=False)
            self._conn.execute(
                "INSERT OR REPLACE INTO file_meta (path, mtime_ns, size) VALUES (?, ?, ?)",
                (str(file_path), mtime_ns, size),
            )
            reindexed += 1

        # Remove entries for deleted files
        stored_paths = {r[0] for r in self._conn.execute("SELECT path FROM file_meta").fetchall()}
        for stale_path in stored_paths - current_paths:
            # Determine date_str from stored path
            stale = Path(stale_path)
            ds = "long_term" if stale.name == "MEMORY.md" else stale.stem
            self._conn.execute("DELETE FROM memory_fts WHERE date_str = ?", (ds,))
            self._conn.execute("DELETE FROM file_meta WHERE path = ?", (stale_path,))
            reindexed += 1

        self._conn.commit()
        return reindexed

    def reindex_file(self, path: Path, date_str: str, *, _commit: bool = True) -> None:
        """Delete all rows for date_str and re-parse the file.

        Args:
            _commit: If False, skip the commit (caller is responsible). Used by
                     reindex_if_needed to batch commits.
        """
        if not self._available or self._conn is None:
            return
        source = "long_term" if date_str == "long_term" else "daily"
        self._conn.execute("DELETE FROM memory_fts WHERE date_str = ?", (date_str,))
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self._conn.execute(
                "INSERT INTO memory_fts (source, date_str, line_number, content) "
                "VALUES (?, ?, ?, ?)",
                (source, date_str, str(i), stripped),
            )
        if _commit:
            self._conn.commit()

    def index_entry(self, source: str, date_str: str, line_number: int, content: str) -> None:
        """Insert a single entry into the FTS index."""
        if not self._available or self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO memory_fts (source, date_str, line_number, content) "
                "VALUES (?, ?, ?, ?)",
                (source, date_str, str(line_number), content.strip()),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("FTS index_entry failed: %s", exc)

    def remove_file(self, date_str: str) -> None:
        """Remove all entries for a given date_str from the index."""
        if not self._available or self._conn is None:
            return
        try:
            self._conn.execute("DELETE FROM memory_fts WHERE date_str = ?", (date_str,))
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("FTS remove_file failed: %s", exc)

    def search(
        self, query: str, max_results: int = 20, today: date | None = None
    ) -> list[tuple[str, int, str, float]]:
        """FTS5 MATCH search with BM25 ranking and temporal decay.

        Args:
            today: Reference date for recency weighting. Defaults to UTC today.

        Returns list of (date_str, line_number, content, combined_score),
        sorted by descending score.
        """
        if not self._available or self._conn is None:
            return []
        if today is None:
            today = datetime.now(UTC).date()

        # Try FTS5 MATCH first; fall back to phrase search on syntax error.
        # Escape double quotes in the fallback to prevent invalid FTS5 syntax.
        escaped = query.replace('"', '""')
        rows: list[tuple] = []
        for attempt_query in (query, f'"{escaped}"'):
            try:
                rows = self._conn.execute(
                    "SELECT date_str, line_number, content, bm25(memory_fts) "
                    "FROM memory_fts WHERE memory_fts MATCH ? "
                    "ORDER BY bm25(memory_fts) LIMIT ?",
                    (attempt_query, max_results * 3),
                ).fetchall()
                break
            except sqlite3.OperationalError:
                continue

        if not rows:
            return []

        scored: list[tuple[str, int, str, float]] = []
        for date_str, line_num, content, bm25_score in rows:
            weight = _recency_weight(date_str, today, self._half_life)
            combined = -bm25_score * weight  # bm25() returns negative values
            scored.append((date_str, int(line_num), content, combined))

        scored.sort(key=lambda x: x[3], reverse=True)
        return scored[:max_results]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class MemoryManager:
    """Manages file-based memory in a workspace directory."""

    def __init__(
        self,
        workspace_dir: str,
        timezone_name: str = "UTC",
        max_daily_entries: int = 50,
        max_long_term_lines: int = 500,
        fts_enabled: bool = True,
        recency_half_life_days: float = 30.0,
    ):
        self._workspace = Path(workspace_dir)
        self._memory_dir = self._workspace / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._max_daily_entries = max_daily_entries
        self._max_long_term_lines = max_long_term_lines
        self._tz: tzinfo
        try:
            self._tz = ZoneInfo(timezone_name)
        except (KeyError, ValueError):
            self._tz = UTC

        # FTS5 index (rebuildable cache)
        self._index: MemoryIndex | None = None
        if fts_enabled:
            try:
                db_path = self._workspace / ".memory_index.sqlite"
                self._index = MemoryIndex(db_path, recency_half_life_days=recency_half_life_days)
                if not self._index.available:
                    self._index = None
            except (sqlite3.Error, OSError):
                logger.warning("Failed to initialize FTS index", exc_info=True)
                self._index = None

    @property
    def long_term_path(self) -> Path:
        return self._workspace / "MEMORY.md"

    def daily_path(self, d: date | None = None) -> Path:
        if d is None:
            d = datetime.now(self._tz).date()
        return self._memory_dir / f"{d.isoformat()}.md"

    def rebuild_index(self) -> int:
        """Rebuild the FTS index from memory files on disk. Returns count of files re-indexed."""
        if self._index is None or not self._index.available:
            return 0
        return self._index.reindex_if_needed(self._memory_dir, self.long_term_path)

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

        # Rate limit: count existing entries and total lines in today's file
        existing_lines = 0
        if path.exists():
            file_lines = path.read_text().splitlines()
            existing_lines = len(file_lines)
            entry_count = sum(1 for line in file_lines if line.startswith("- ["))
            if entry_count >= self._max_daily_entries:
                logger.warning("Daily memory limit reached (%d entries)", self._max_daily_entries)
                return f"Daily memory limit reached ({self._max_daily_entries} entries). Try again tomorrow."

        timestamp = today.strftime("%H:%M")
        entry = f"- [{timestamp}] **{category}**: {text}\n"

        # Create file with header if new
        if not path.exists():
            header = f"# Memory — {today.date().isoformat()}\n\n"
            path.write_text(header)
            existing_lines = len(header.splitlines())

        with open(path, "a") as f:
            f.write(entry)

        # Update FTS index (line number = existing lines + 1 for the new entry)
        if self._index and self._index.available:
            self._index.index_entry(
                "daily", today.date().isoformat(), existing_lines + 1, entry.rstrip("\n")
            )

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

        # Update FTS index (line_count was read before the write; the write adds
        # "\n{text}\n" so the new entry lands on line_count + 1 + text's line count,
        # but since index_entry only needs an approximate line ref, use line_count + 2).
        if self._index and self._index.available:
            self._index.index_entry("long_term", "long_term", line_count + 2, text)

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
        """Search across all memory files.

        Uses FTS5 full-text search with BM25 ranking and temporal decay when
        available, otherwise falls back to case-insensitive substring search.

        Returns results formatted as [YYYY-MM-DD L{n}] entry text so the
        LLM can reference them in edit/delete calls.
        """
        if self._index and self._index.available:
            return self._search_fts(query, max_results)
        return self._search_substring(query, max_results)

    def _search_fts(self, query: str, max_results: int) -> str:
        """FTS5-based search with BM25 ranking and recency weighting."""
        if self._index is None:  # pragma: no cover — guarded by caller
            return self._search_substring(query, max_results)
        today = datetime.now(self._tz).date()
        hits = self._index.search(query, max_results=max_results, today=today)
        if not hits:
            return f"No memories found matching '{query}'."
        results = [f"[{ds} L{ln}] {content}" for ds, ln, content, _score in hits]
        return f"Found {len(results)} result(s):\n" + "\n".join(results)

    def _search_substring(self, query: str, max_results: int) -> str:
        """Case-insensitive substring search (fallback when FTS5 unavailable)."""
        query_lower = query.lower()
        results: list[str] = []

        # Search daily files, newest first
        daily_files = sorted(self._memory_dir.glob("*.md"), reverse=True)
        for path in daily_files:
            try:
                lines = path.read_text().splitlines()
            except OSError:
                continue
            date_str = path.stem
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

        # Re-index the affected file (line numbers shifted)
        if self._index and self._index.available:
            self._index.reindex_file(path, date_str)

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

        # Re-index the affected file
        if self._index and self._index.available:
            self._index.reindex_file(path, date_str)

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

    def compact_daily_files(self, days_to_keep: int = 7, summarize: bool = True) -> str:
        """Remove daily files older than days_to_keep.

        When summarize=True (default), appends extractive summary bullets to
        MEMORY.md preserving actual entry content. When False, appends only
        a count-only line (legacy behavior).

        For empty files (header only): just deletes.

        Args:
            days_to_keep: Number of recent days to preserve.
            summarize: If True, write entry text bullets; if False, count-only.

        Returns:
            Summary of compaction results.
        """
        today = datetime.now(self._tz).date()
        cutoff = today - timedelta(days=days_to_keep)
        compacted = 0
        skipped_summaries = 0

        daily_files = sorted(self._memory_dir.glob("*.md"))
        for path in daily_files:
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_date >= cutoff:
                continue

            # Read entries
            try:
                content = path.read_text()
            except OSError:
                continue
            entry_lines = [line for line in content.splitlines() if line.startswith("- [")]
            entry_count = len(entry_lines)

            if entry_count > 0:
                # Append to MEMORY.md
                lt_path = self.long_term_path
                if not lt_path.exists():
                    lt_path.write_text("# Long-Term Memory\n\n")

                # Safety: skip writing if MEMORY.md is over 2x max_long_term_lines.
                # Note: read-then-append is not atomic, but compaction only runs at
                # startup so concurrent access is not expected.
                lt_lines = len(lt_path.read_text().splitlines())
                if lt_lines < self._max_long_term_lines * 2:
                    with open(lt_path, "a") as f:
                        if summarize:
                            # Dedupe entries with identical text after prefix stripping
                            # (different timestamps but same content collapse to one)
                            entries = [_strip_entry_prefix(line) for line in entry_lines]
                            entries = list(dict.fromkeys(entries))
                            f.write(
                                f"\n### Compacted: {file_date.isoformat()}"
                                f" ({len(entries)} entries)\n"
                            )
                            for e in entries:
                                f.write(f"- {e}\n")
                        else:
                            f.write(
                                f"- [{file_date.isoformat()}] {entry_count} entries (compacted)\n"
                            )
                else:
                    skipped_summaries += 1
                    logger.warning(
                        "MEMORY.md exceeds safety limit (%d lines, max %d); "
                        "keeping daily file %s to avoid data loss (%d entries)",
                        lt_lines,
                        self._max_long_term_lines * 2,
                        file_date.isoformat(),
                        entry_count,
                    )
                    continue  # keep the daily file — don't delete what we can't preserve

            path.unlink()
            compacted += 1
            logger.info("Compacted memory file: %s (%d entries)", path.name, entry_count)

            # Remove from FTS index
            if self._index and self._index.available:
                self._index.remove_file(file_date.isoformat())

        if compacted == 0 and skipped_summaries == 0:
            return "No files to compact."
        parts: list[str] = []
        if compacted:
            parts.append(f"Compacted {compacted} file(s) older than {days_to_keep} days.")
        if skipped_summaries:
            parts.append(
                f"Warning: {skipped_summaries} file(s) kept (not compacted)"
                f" because MEMORY.md exceeds size limit."
            )
        return " ".join(parts)

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
