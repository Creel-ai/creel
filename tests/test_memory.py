"""Tests for the memory system."""

import os
import sqlite3
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from creel.memory import MemoryIndex, MemoryManager, _strip_entry_prefix
from creel.models import WorkspaceConfig


class TestMemoryManager:
    def _make_manager(self, td: str, **kwargs) -> MemoryManager:
        return MemoryManager(workspace_dir=td, timezone_name="UTC", **kwargs)

    def test_remember_creates_daily_file(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            result = mm.remember("User likes coffee", "preference")
            assert "Remembered" in result
            # Check file exists — use the manager's own daily_path to avoid timezone mismatch
            path = mm.daily_path()
            assert path.exists()
            content = path.read_text()
            assert "coffee" in content
            assert "preference" in content

    def test_remember_appends(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("First note")
            mm.remember("Second note")
            path = mm.daily_path()
            content = path.read_text()
            assert "First note" in content
            assert "Second note" in content

    def test_update_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            result = mm.update_long_term("Important fact")
            assert "updated" in result.lower()
            path = Path(td) / "MEMORY.md"
            assert path.exists()
            assert "Important fact" in path.read_text()

    def test_get_recent_context_empty(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            result = mm.get_recent_context()
            assert result is None

    def test_get_recent_context_with_daily(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("Test memory entry")
            result = mm.get_recent_context()
            assert result is not None
            assert "Test memory entry" in result

    def test_get_recent_context_with_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.update_long_term("Long-term fact")
            result = mm.get_recent_context()
            assert result is not None
            assert "Long-term fact" in result

    def test_get_recent_context_max_chars(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("x" * 500)
            result = mm.get_recent_context(max_chars=200)
            assert result is not None
            assert len(result) <= 300  # some overhead for headers

    def test_daily_path(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            path = mm.daily_path(date(2026, 1, 15))
            assert path.name == "2026-01-15.md"

    # --- get_relevant_context ---

    def test_get_relevant_context_includes_today(self):
        """Today's entries should always be included regardless of query."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("Meeting with Alice about project X")
            result = mm.get_relevant_context("unrelated query")
            assert result is not None
            # Today's entries are always included for continuity
            assert "Meeting with Alice" in result

    def test_get_relevant_context_finds_matching_entries(self):
        """Search results from older days should be included when they match."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            # Write an older daily file with matching content
            mem_dir = Path(td) / "memory"
            old_file = mem_dir / "2026-01-15.md"
            old_file.write_text(
                "# Memory — 2026-01-15\n\n"
                "- [10:00] **general**: Budget review for Q1\n"
                "- [11:00] **general**: Lunch at noon\n"
            )
            mm.rebuild_index()
            result = mm.get_relevant_context("budget review")
            assert result is not None
            assert "Budget review" in result

    def test_get_relevant_context_empty_when_nothing_matches(self):
        """Returns None when no entries match and no today file exists."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            result = mm.get_relevant_context("completely nonexistent topic")
            assert result is None

    def test_get_relevant_context_respects_max_chars(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("x" * 500)
            result = mm.get_relevant_context("anything", max_chars=200)
            assert result is not None
            assert len(result) <= 220  # max_chars + truncation marker overhead

    def test_get_relevant_context_excludes_today_from_search(self):
        """Today's entries from search results should not be duplicated."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("Deploy to production at 3pm")
            mm.rebuild_index()
            result = mm.get_relevant_context("deploy production")
            assert result is not None
            # "Deploy" should appear once (from today section), not duplicated
            assert result.count("Deploy to production") == 1

    # --- search_memory ---

    def test_search_finds_entries(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("User likes coffee", "preference")
            mm.remember("Meeting at 3pm", "schedule")
            result = mm.search_memory("coffee")
            assert "1 result" in result
            assert "coffee" in result

    def test_search_no_results(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("User likes coffee")
            result = mm.search_memory("zebra")
            assert "No memories found" in result

    def test_search_includes_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.update_long_term("- [note] Important long-term fact")
            result = mm.search_memory("Important")
            assert "long_term" in result
            assert "1 result" in result

    def test_search_respects_max_results(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            for i in range(10):
                mm.remember(f"Note number {i}")
            result = mm.search_memory("Note", max_results=3)
            assert "3 result" in result

    def test_search_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("User likes COFFEE")
            result = mm.search_memory("coffee")
            assert "1 result" in result

    # --- delete_memory ---

    def test_delete_removes_entry(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("Delete me")
            path = mm.daily_path()
            # Find the line number of the entry (after header)
            lines = path.read_text().splitlines()
            entry_line = next(i for i, line in enumerate(lines, 1) if "Delete me" in line)
            today_str = datetime.now(UTC).date().isoformat()
            result = mm.delete_memory(today_str, entry_line)
            assert "Deleted" in result
            assert "Delete me" not in path.read_text()

    def test_delete_invalid_date(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            result = mm.delete_memory("not-a-date", 1)
            assert "Invalid date" in result

    def test_delete_line_out_of_range(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("Only entry")
            today_str = datetime.now(UTC).date().isoformat()
            result = mm.delete_memory(today_str, 999)
            assert "out of range" in result

    def test_delete_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.update_long_term("- Line to delete")
            # Find the line number
            lines = mm.long_term_path.read_text().splitlines()
            entry_line = next(i for i, line in enumerate(lines, 1) if "Line to delete" in line)
            result = mm.delete_memory("long_term", entry_line)
            assert "Deleted" in result
            assert "Line to delete" not in mm.long_term_path.read_text()

    # --- edit_memory ---

    def test_edit_replaces_line(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("Old text")
            path = mm.daily_path()
            lines = path.read_text().splitlines()
            entry_line = next(i for i, line in enumerate(lines, 1) if "Old text" in line)
            today_str = datetime.now(UTC).date().isoformat()
            result = mm.edit_memory(today_str, entry_line, "- [10:00] **general**: New text")
            assert "Edited" in result
            assert "old: " in result
            assert "new: " in result
            assert "New text" in path.read_text()
            assert "Old text" not in path.read_text()

    def test_edit_invalid_line(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("Entry")
            today_str = datetime.now(UTC).date().isoformat()
            result = mm.edit_memory(today_str, 999, "replacement")
            assert "out of range" in result

    def test_edit_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.update_long_term("- Old fact")
            lines = mm.long_term_path.read_text().splitlines()
            entry_line = next(i for i, line in enumerate(lines, 1) if "Old fact" in line)
            result = mm.edit_memory("long_term", entry_line, "- New fact")
            assert "Edited" in result
            assert "New fact" in mm.long_term_path.read_text()

    # --- list_memory_files ---

    def test_list_empty_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            result = mm.list_memory_files()
            assert "No memory files" in result

    def test_list_with_daily_entries(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("An entry")
            result = mm.list_memory_files()
            assert "Memory files:" in result
            assert "1 entries" in result

    def test_list_includes_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.update_long_term("A fact")
            result = mm.list_memory_files()
            assert "MEMORY.md" in result

    # --- compact_daily_files ---

    def test_compact_removes_old_files(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            # Create an old daily file
            old_date = datetime.now(UTC).date() - timedelta(days=10)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n- [10:00] **general**: Old note\n"
            )
            result = mm.compact_daily_files(days_to_keep=7)
            assert "Compacted 1" in result
            assert not old_path.exists()

    def test_compact_preserves_recent(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("Recent note")
            result = mm.compact_daily_files(days_to_keep=7)
            assert "No files to compact" in result
            assert mm.daily_path().exists()

    def test_compact_appends_summary_to_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            old_date = datetime.now(UTC).date() - timedelta(days=10)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n"
                f"- [10:00] **general**: Note 1\n- [11:00] **general**: Note 2\n"
            )
            # Default summarize=True: should write entry text bullets
            mm.compact_daily_files(days_to_keep=7)
            lt_content = mm.long_term_path.read_text()
            assert f"### Compacted: {old_date.isoformat()}" in lt_content
            assert "- Note 1" in lt_content
            assert "- Note 2" in lt_content

    def test_compact_summarize_false(self):
        """When summarize=False, use legacy count-only format."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            old_date = datetime.now(UTC).date() - timedelta(days=10)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n"
                f"- [10:00] **general**: Note 1\n- [11:00] **general**: Note 2\n"
            )
            mm.compact_daily_files(days_to_keep=7, summarize=False)
            lt_content = mm.long_term_path.read_text()
            assert "2 entries (compacted)" in lt_content

    def test_compact_deletes_empty_files(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            old_date = datetime.now(UTC).date() - timedelta(days=10)
            old_path = mm.daily_path(old_date)
            old_path.write_text(f"# Memory — {old_date.isoformat()}\n\n")
            mm.compact_daily_files(days_to_keep=7)
            assert not old_path.exists()
            # No summary appended for empty files
            assert not mm.long_term_path.exists()

    # --- Rate limiting ---

    def test_daily_limit_rejects_when_full(self):
        with tempfile.TemporaryDirectory() as td:
            mm = MemoryManager(workspace_dir=td, timezone_name="UTC", max_daily_entries=3)
            mm.remember("Entry 1")
            mm.remember("Entry 2")
            mm.remember("Entry 3")
            result = mm.remember("Entry 4 should fail")
            assert "limit reached" in result
            # Verify only 3 entries in the file
            content = mm.daily_path().read_text()
            assert content.count("- [") == 3

    def test_daily_limit_allows_under_limit(self):
        with tempfile.TemporaryDirectory() as td:
            mm = MemoryManager(workspace_dir=td, timezone_name="UTC", max_daily_entries=5)
            result = mm.remember("Entry 1")
            assert "Remembered" in result
            result = mm.remember("Entry 2")
            assert "Remembered" in result

    def test_long_term_limit_rejects_when_full(self):
        with tempfile.TemporaryDirectory() as td:
            mm = MemoryManager(workspace_dir=td, timezone_name="UTC", max_long_term_lines=5)
            # Header takes 2 lines ("# Long-Term Memory\n\n"), then content lines
            mm.update_long_term("Line 1")
            mm.update_long_term("Line 2")
            # After header (2 lines) + blank + "Line 1" + blank + "Line 2" = 6 lines
            result = mm.update_long_term("Line 3 should fail")
            assert "limit reached" in result

    def test_long_term_limit_allows_under_limit(self):
        with tempfile.TemporaryDirectory() as td:
            mm = MemoryManager(workspace_dir=td, timezone_name="UTC", max_long_term_lines=200)
            result = mm.update_long_term("Some fact")
            assert "updated" in result.lower()


class TestStripEntryPrefix:
    def test_strips_standard_prefix(self):
        assert _strip_entry_prefix("- [10:00] **general**: Hello world") == "Hello world"

    def test_preserves_non_entry_lines(self):
        assert _strip_entry_prefix("Just a plain line") == "Just a plain line"

    def test_strips_various_categories(self):
        assert _strip_entry_prefix("- [14:30] **preference**: Likes coffee") == "Likes coffee"

    def test_strips_hyphenated_category(self):
        assert _strip_entry_prefix("- [09:00] **long-term**: Important fact") == "Important fact"


class TestMemoryIndex:
    def _make_index(self, td: str, **kwargs) -> MemoryIndex:
        db_path = Path(td) / ".memory_index.sqlite"
        return MemoryIndex(db_path, **kwargs)

    def test_index_creation(self):
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            assert idx.available
            idx.close()

    def test_index_entry_and_search(self):
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            idx.index_entry("daily", "2026-01-15", 3, "User likes coffee in the morning")
            results = idx.search("coffee", today=date(2026, 1, 15))
            assert len(results) == 1
            assert results[0][0] == "2026-01-15"
            assert results[0][1] == 3
            assert "coffee" in results[0][2]
            idx.close()

    def test_search_bm25_ranking(self):
        """More relevant entries should rank higher."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            idx.index_entry("daily", "2026-01-15", 1, "coffee coffee coffee is great")
            idx.index_entry("daily", "2026-01-15", 2, "I had coffee once")
            results = idx.search("coffee", today=date(2026, 1, 15))
            assert len(results) == 2
            # Entry with more "coffee" mentions should score higher
            assert "coffee coffee coffee" in results[0][2]
            idx.close()

    def test_search_recency_weighting(self):
        """Recent entries should rank higher than old ones for same relevance."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td, recency_half_life_days=7.0)
            idx.index_entry("daily", "2026-01-01", 1, "Meeting about project alpha")
            idx.index_entry("daily", "2026-03-10", 1, "Meeting about project alpha")
            results = idx.search("project alpha", today=date(2026, 3, 12))
            assert len(results) == 2
            # Recent entry should be first
            assert results[0][0] == "2026-03-10"
            assert results[0][3] > results[1][3]
            idx.close()

    def test_search_evergreen_no_decay(self):
        """long_term entries should not be affected by temporal decay."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td, recency_half_life_days=1.0)
            idx.index_entry("long_term", "long_term", 1, "Evergreen important fact")
            results = idx.search("evergreen", today=date(2026, 3, 12))
            assert len(results) == 1
            assert results[0][3] > 0
            idx.close()

    def test_reindex_if_needed(self):
        """Only changed files should be re-indexed."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            mem_dir = Path(td) / "memory"
            mem_dir.mkdir()
            lt_path = Path(td) / "MEMORY.md"

            # Create a daily file
            daily = mem_dir / "2026-01-15.md"
            daily.write_text("# Memory\n\n- [10:00] **general**: Hello world\n")

            count = idx.reindex_if_needed(mem_dir, lt_path)
            assert count == 1

            # Re-index without changes — should return 0
            count = idx.reindex_if_needed(mem_dir, lt_path)
            assert count == 0

            idx.close()

    def test_reindex_skips_when_only_mtime_changes(self):
        """When file is touched but content unchanged, skip reindexing."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            mem_dir = Path(td) / "memory"
            mem_dir.mkdir()
            lt_path = Path(td) / "MEMORY.md"

            daily = mem_dir / "2026-01-15.md"
            daily.write_text("# Memory\n\n- [10:00] **general**: Stable content\n")

            count = idx.reindex_if_needed(mem_dir, lt_path)
            assert count == 1

            # Touch the file (changes mtime, same content)
            os.utime(daily, None)

            count = idx.reindex_if_needed(mem_dir, lt_path)
            assert count == 0  # hash matched — no reindex

            # Content should still be searchable
            results = idx.search("Stable content", today=date(2026, 1, 15))
            assert len(results) == 1
            idx.close()

    def test_reindex_triggers_on_content_change(self):
        """When file content actually changes, reindex should fire."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            mem_dir = Path(td) / "memory"
            mem_dir.mkdir()
            lt_path = Path(td) / "MEMORY.md"

            daily = mem_dir / "2026-01-15.md"
            daily.write_text("# Memory\n\n- [10:00] **general**: Original text\n")

            count = idx.reindex_if_needed(mem_dir, lt_path)
            assert count == 1

            # Change actual content
            daily.write_text("# Memory\n\n- [10:00] **general**: Updated text\n")

            count = idx.reindex_if_needed(mem_dir, lt_path)
            assert count == 1  # hash differs — reindexed

            results = idx.search("Updated text", today=date(2026, 1, 15))
            assert len(results) == 1
            results = idx.search("Original text", today=date(2026, 1, 15))
            assert len(results) == 0
            idx.close()

    def test_reindex_schema_migration(self):
        """Old DBs without content_hash column should be migrated transparently."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.sqlite"
            # Create DB with old schema (no content_hash column)
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE VIRTUAL TABLE memory_fts USING fts5("
                "source, date_str, line_number, content, "
                "tokenize='porter unicode61')"
            )
            conn.execute(
                "CREATE TABLE file_meta ("
                "path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, "
                "size INTEGER NOT NULL)"
            )
            conn.commit()
            conn.close()

            # Opening with MemoryIndex should migrate and work fine
            idx = MemoryIndex(db_path)
            assert idx.available

            mem_dir = Path(td) / "memory"
            mem_dir.mkdir()
            lt_path = Path(td) / "MEMORY.md"

            daily = mem_dir / "2026-01-15.md"
            daily.write_text("# Memory\n\n- [10:00] **general**: After migration\n")

            count = idx.reindex_if_needed(mem_dir, lt_path)
            assert count == 1

            results = idx.search("After migration", today=date(2026, 1, 15))
            assert len(results) == 1
            idx.close()

    def test_reindex_removes_deleted_files(self):
        """Stale entries should be cleaned up when files are deleted."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            mem_dir = Path(td) / "memory"
            mem_dir.mkdir()
            lt_path = Path(td) / "MEMORY.md"

            daily = mem_dir / "2026-01-15.md"
            daily.write_text("# Memory\n\n- [10:00] **general**: Test entry\n")
            idx.reindex_if_needed(mem_dir, lt_path)

            # Verify searchable
            results = idx.search("Test entry", today=date(2026, 1, 15))
            assert len(results) == 1

            # Delete file and re-index
            daily.unlink()
            count = idx.reindex_if_needed(mem_dir, lt_path)
            assert count == 1  # one stale file removed

            results = idx.search("Test entry", today=date(2026, 1, 15))
            assert len(results) == 0
            idx.close()

    def test_remove_file(self):
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            idx.index_entry("daily", "2026-01-15", 1, "Entry to remove")
            idx.remove_file("2026-01-15")
            results = idx.search("remove", today=date(2026, 1, 15))
            assert len(results) == 0
            idx.close()

    def test_reindex_file(self):
        """Re-parsing a single file should replace all its entries."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            mem_dir = Path(td) / "memory"
            mem_dir.mkdir()

            daily = mem_dir / "2026-01-15.md"
            daily.write_text("# Memory\n\n- [10:00] **general**: Original entry\n")
            idx.reindex_file(daily, "2026-01-15")

            results = idx.search("Original", today=date(2026, 1, 15))
            assert len(results) == 1

            # Overwrite file and re-index
            daily.write_text("# Memory\n\n- [10:00] **general**: Replaced entry\n")
            idx.reindex_file(daily, "2026-01-15")

            results = idx.search("Original", today=date(2026, 1, 15))
            assert len(results) == 0
            results = idx.search("Replaced", today=date(2026, 1, 15))
            assert len(results) == 1
            idx.close()

    def test_search_fts_syntax_fallback(self):
        """Special characters shouldn't crash — should fall back to phrase search."""
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            idx.index_entry("daily", "2026-01-15", 1, "test with special chars: a+b")
            # Query with FTS5 special chars should not raise
            results = idx.search("a+b", today=date(2026, 1, 15))
            assert isinstance(results, list)
            # The phrase fallback should find the indexed content
            if results:
                assert "a+b" in results[0][2]
            idx.close()

    def test_search_empty_index(self):
        with tempfile.TemporaryDirectory() as td:
            idx = self._make_index(td)
            results = idx.search("anything", today=date(2026, 1, 15))
            assert results == []
            idx.close()

    def test_close_and_reopen(self):
        """Data should persist across close/reopen."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / ".memory_index.sqlite"
            idx = MemoryIndex(db_path)
            idx.index_entry("daily", "2026-01-15", 1, "Persistent data")
            idx.close()

            idx2 = MemoryIndex(db_path)
            results = idx2.search("Persistent", today=date(2026, 1, 15))
            assert len(results) == 1
            idx2.close()

    def test_unavailable_graceful(self):
        """If FTS5 init fails, available should return False and methods should be safe."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / ".memory_index.sqlite"
            # Create a corrupt database file
            db_path.write_text("not a database")
            idx = MemoryIndex(db_path)
            assert not idx.available
            # Methods should be no-ops
            idx.index_entry("daily", "2026-01-15", 1, "test")
            assert idx.search("test") == []
            idx.close()


class TestExtraPaths:
    """Tests for shared knowledge from extra directories."""

    def _make_manager(self, td: str, **kwargs) -> MemoryManager:
        return MemoryManager(workspace_dir=td, timezone_name="UTC", **kwargs)

    def test_extra_paths_indexed_and_searchable(self):
        """Markdown files in extra_paths should be indexed and searchable."""
        with tempfile.TemporaryDirectory() as td:
            # Create a shared docs directory outside workspace
            shared_dir = Path(td) / "shared_docs"
            shared_dir.mkdir()
            (shared_dir / "runbook.md").write_text(
                "# Runbook\n\nRestart the API server with `systemctl restart api`.\n"
            )

            mm = self._make_manager(td, extra_paths=[str(shared_dir)])
            mm.rebuild_index()

            result = mm.search_memory("restart API server")
            assert "systemctl restart api" in result

    def test_extra_paths_in_substring_fallback(self):
        """Extra paths should be searched even without FTS."""
        with tempfile.TemporaryDirectory() as td:
            shared_dir = Path(td) / "shared_docs"
            shared_dir.mkdir()
            (shared_dir / "faq.md").write_text("# FAQ\n\nThe deploy key is stored in 1Password.\n")

            mm = self._make_manager(td, fts_enabled=False, extra_paths=[str(shared_dir)])
            result = mm.search_memory("deploy key")
            assert "1Password" in result

    def test_extra_paths_recursive(self):
        """Nested subdirectories in extra_paths should be searched."""
        with tempfile.TemporaryDirectory() as td:
            shared_dir = Path(td) / "docs"
            (shared_dir / "ops").mkdir(parents=True)
            (shared_dir / "ops" / "alerts.md").write_text(
                "# Alerts\n\nPage oncall if error rate exceeds 5%.\n"
            )

            mm = self._make_manager(td, extra_paths=[str(shared_dir)])
            mm.rebuild_index()

            result = mm.search_memory("error rate")
            assert "oncall" in result

    def test_extra_paths_missing_dir_ignored(self):
        """Non-existent extra_paths should be silently skipped."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, extra_paths=["/nonexistent/path"])
            count = mm.rebuild_index()
            assert count == 0

    def test_extra_paths_in_relevant_context(self):
        """Extra path content should appear in relevant context results."""
        with tempfile.TemporaryDirectory() as td:
            shared_dir = Path(td) / "team_docs"
            shared_dir.mkdir()
            (shared_dir / "processes.md").write_text(
                "# Processes\n\nCode review requires two approvals before merge.\n"
            )

            mm = self._make_manager(td, extra_paths=[str(shared_dir)])
            mm.rebuild_index()

            result = mm.get_relevant_context("code review approvals")
            assert result is not None
            assert "two approvals" in result


class TestMemoryManagerFTS:
    """Integration tests for MemoryManager with FTS index."""

    def _make_manager(self, td: str, **kwargs) -> MemoryManager:
        return MemoryManager(workspace_dir=td, timezone_name="UTC", **kwargs)

    def test_search_uses_fts(self):
        """remember + FTS search finds entries."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=True)
            mm.remember("User loves espresso drinks", "preference")
            # FTS should find this even with stemming
            result = mm.search_memory("espresso")
            assert "1 result" in result
            assert "espresso" in result

    def test_search_falls_back_without_fts(self):
        """fts_enabled=False uses substring search."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=False)
            mm.remember("User likes coffee")
            result = mm.search_memory("coffee")
            assert "1 result" in result

    def test_remember_updates_index(self):
        """Entry should be immediately searchable via FTS after remember()."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=True)
            mm.remember("Quarterly planning meeting", "schedule")
            result = mm.search_memory("quarterly planning")
            assert "1 result" in result

    def test_delete_updates_index(self):
        """Deleted entry should not be found in FTS."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=True)
            mm.remember("Delete this entry")
            path = mm.daily_path()
            lines = path.read_text().splitlines()
            entry_line = next(i for i, line in enumerate(lines, 1) if "Delete this" in line)
            today_str = datetime.now(UTC).date().isoformat()
            mm.delete_memory(today_str, entry_line)
            result = mm.search_memory("Delete this entry")
            assert "No memories found" in result

    def test_edit_updates_index(self):
        """After edit, old text gone and new text found."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=True)
            mm.remember("Original unique phrase")
            path = mm.daily_path()
            lines = path.read_text().splitlines()
            entry_line = next(i for i, line in enumerate(lines, 1) if "Original unique" in line)
            today_str = datetime.now(UTC).date().isoformat()
            mm.edit_memory(
                today_str, entry_line, "- [10:00] **general**: Replacement unique phrase"
            )
            result = mm.search_memory("Original unique")
            assert "No memories found" in result
            result = mm.search_memory("Replacement unique")
            assert "1 result" in result

    def test_compact_removes_from_index(self):
        """Compacted entries should be removed from FTS."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=True)
            old_date = datetime.now(UTC).date() - timedelta(days=10)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n"
                f"- [10:00] **general**: Unique compaction test entry\n"
            )
            # Index the old file
            mm.rebuild_index()
            result = mm.search_memory("Unique compaction test")
            assert "1 result" in result

            mm.compact_daily_files(days_to_keep=7)
            result = mm.search_memory("Unique compaction test")
            assert "No memories found" in result

    def test_compact_summarize_true(self):
        """MEMORY.md should have entry text bullets when summarize=True."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            old_date = datetime.now(UTC).date() - timedelta(days=10)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n"
                f"- [10:00] **general**: Alpha note\n- [11:00] **general**: Beta note\n"
            )
            mm.compact_daily_files(days_to_keep=7, summarize=True)
            lt_content = mm.long_term_path.read_text()
            assert "### Compacted:" in lt_content
            assert "- Alpha note" in lt_content
            assert "- Beta note" in lt_content

    def test_compact_summarize_false_legacy(self):
        """MEMORY.md should have count-only line when summarize=False."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            old_date = datetime.now(UTC).date() - timedelta(days=10)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n"
                f"- [10:00] **general**: Note 1\n- [11:00] **general**: Note 2\n"
            )
            mm.compact_daily_files(days_to_keep=7, summarize=False)
            lt_content = mm.long_term_path.read_text()
            assert "2 entries (compacted)" in lt_content

    def test_rebuild_index(self):
        """rebuild_index should index existing files."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=True)
            # Write a file directly (bypassing remember)
            path = mm.daily_path(date(2026, 1, 15))
            path.write_text("# Memory — 2026-01-15\n\n- [10:00] **general**: Rebuild test entry\n")
            count = mm.rebuild_index()
            assert count == 1

    def test_search_with_embedded_quotes(self):
        """Queries containing unbalanced quotes should not crash FTS."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=True)
            mm.remember('He said "hello there', "quote")
            result = mm.search_memory('he said "hello')
            # Should not crash; may or may not find results
            assert isinstance(result, str)

    def test_compact_keeps_file_when_memory_full(self):
        """When MEMORY.md exceeds 2x limit, daily file is kept to avoid data loss."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, max_long_term_lines=5)
            # Fill MEMORY.md past 2x limit (10 lines)
            lt_path = mm.long_term_path
            lt_path.write_text("# Long-Term Memory\n\n" + "- line\n" * 10)

            old_date = datetime.now(UTC).date() - timedelta(days=10)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n- [10:00] **general**: Preserved entry\n"
            )
            result = mm.compact_daily_files(days_to_keep=7)
            assert "Warning" in result
            assert "kept" in result
            # The daily file should be preserved — not deleted
            assert old_path.exists()
            assert "Preserved entry" in old_path.read_text()

    def test_reindex_skips_non_date_files(self):
        """Non-date .md files in memory dir should not pollute the index."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, fts_enabled=True)
            # Create a non-date file
            readme = mm._memory_dir / "README.md"
            readme.write_text("# Not a daily file\n\nSome content\n")
            # Create a valid daily file
            mm.remember("Valid entry")
            count = mm.rebuild_index()
            # Only the daily file should be indexed, not README.md
            assert count == 1


class TestSubstringRecencyWeighting:
    """Tests for recency-weighted substring search (fallback path)."""

    def _make_manager(self, td: str, **kwargs) -> MemoryManager:
        return MemoryManager(workspace_dir=td, timezone_name="UTC", fts_enabled=False, **kwargs)

    def test_recent_entries_rank_higher(self):
        """With short half-life, recent entries should sort above old ones."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, recency_half_life_days=7.0)
            today = datetime.now(UTC).date()

            old_date = today - timedelta(days=60)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n"
                f"- [10:00] **general**: Meeting about project alpha\n"
            )

            recent_date = today - timedelta(days=2)
            recent_path = mm.daily_path(recent_date)
            recent_path.write_text(
                f"# Memory — {recent_date.isoformat()}\n\n"
                f"- [10:00] **general**: Meeting about project alpha\n"
            )

            result = mm.search_memory("project alpha")
            assert "2 result" in result
            lines = result.split("\n")
            # Recent entry should appear first
            assert recent_date.isoformat() in lines[1]
            assert old_date.isoformat() in lines[2]

    def test_long_term_evergreen_beats_decayed(self):
        """Long-term entries (weight 1.0) should rank above heavily-decayed daily entries."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, recency_half_life_days=1.0)
            today = datetime.now(UTC).date()

            old_date = today - timedelta(days=30)
            old_path = mm.daily_path(old_date)
            old_path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n"
                f"- [10:00] **general**: Important fact about widgets\n"
            )

            mm.update_long_term("- Important fact about widgets")

            result = mm.search_memory("widgets")
            assert "2 result" in result
            lines = result.split("\n")
            # Long-term (weight 1.0) should appear before 30-day-old daily (heavily decayed)
            assert "long_term" in lines[1]

    def test_same_day_preserves_line_order(self):
        """Entries from the same date should maintain line order (stable sort)."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            today = datetime.now(UTC).date()
            path = mm.daily_path(today)
            path.write_text(
                f"# Memory — {today.isoformat()}\n\n"
                f"- [10:00] **general**: First note about coffee\n"
                f"- [11:00] **general**: Second note about coffee\n"
                f"- [12:00] **general**: Third note about coffee\n"
            )

            result = mm.search_memory("coffee")
            assert "3 result" in result
            lines = result.split("\n")[1:]  # skip "Found N result(s):" line
            assert "First note" in lines[0]
            assert "Second note" in lines[1]
            assert "Third note" in lines[2]

    def test_max_results_after_ranking(self):
        """Top-N should be by recency, not first-found."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, recency_half_life_days=7.0)
            today = datetime.now(UTC).date()

            # Create entries across multiple dates
            for days_ago in [1, 30, 60]:
                d = today - timedelta(days=days_ago)
                path = mm.daily_path(d)
                path.write_text(
                    f"# Memory — {d.isoformat()}\n\n"
                    f"- [10:00] **general**: Status update from {d.isoformat()}\n"
                )

            result = mm.search_memory("Status update", max_results=2)
            assert "2 result" in result
            lines = result.split("\n")[1:]
            recent = today - timedelta(days=1)
            middle = today - timedelta(days=30)
            assert recent.isoformat() in lines[0]
            assert middle.isoformat() in lines[1]

    def test_skips_non_date_files(self):
        """Non-date .md files in memory dir should not appear in results."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            readme = mm._memory_dir / "README.md"
            readme.write_text("- [10:00] **general**: Sneaky match\n")

            result = mm.search_memory("Sneaky")
            assert "No memories found" in result


class TestCompactLLMSummarize:
    """Tests for the LLM summarization path in compact_daily_files."""

    def _make_manager(self, td: str, **kwargs) -> MemoryManager:
        return MemoryManager(workspace_dir=td, timezone_name="UTC", **kwargs)

    def _create_old_file(self, mm: MemoryManager, days_ago: int = 10) -> date:
        old_date = datetime.now(UTC).date() - timedelta(days=days_ago)
        path = mm.daily_path(old_date)
        path.write_text(
            f"# Memory — {old_date.isoformat()}\n\n"
            f"- [10:00] **general**: Alpha fact\n"
            f"- [11:00] **preference**: Beta detail\n"
            f"- [14:30] **general**: Gamma note\n"
        )
        return old_date

    def test_compact_uses_llm_summarize_fn(self):
        """When callback is provided and succeeds, MEMORY.md gets LLM output."""
        with tempfile.TemporaryDirectory() as td:

            def mock_summarize(entries: list[str]) -> str:
                return "- Key fact A\n- Key fact B\n"

            mm = self._make_manager(td, compact_summarize_fn=mock_summarize)
            old_date = self._create_old_file(mm)
            mm.compact_daily_files(days_to_keep=7, summarize=True)

            lt_content = mm.long_term_path.read_text()
            assert f"### Summarized: {old_date.isoformat()}" in lt_content
            assert "- Key fact A" in lt_content
            assert "- Key fact B" in lt_content
            # Should NOT contain raw extractive bullets
            assert "- Alpha fact" not in lt_content

    def test_compact_llm_fallback_on_error(self):
        """When callback raises, falls back to extractive summarization."""
        with tempfile.TemporaryDirectory() as td:

            def failing_summarize(entries: list[str]) -> str:
                raise RuntimeError("LLM API unavailable")

            mm = self._make_manager(td, compact_summarize_fn=failing_summarize)
            old_date = self._create_old_file(mm)
            mm.compact_daily_files(days_to_keep=7, summarize=True)

            lt_content = mm.long_term_path.read_text()
            # Fallback: extractive with "Compacted" header
            assert f"### Compacted: {old_date.isoformat()}" in lt_content
            assert "- Alpha fact" in lt_content
            assert "- Beta detail" in lt_content

    def test_compact_no_callback_uses_extractive(self):
        """compact_summarize_fn=None with summarize=True uses extractive."""
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td, compact_summarize_fn=None)
            old_date = self._create_old_file(mm)
            mm.compact_daily_files(days_to_keep=7, summarize=True)

            lt_content = mm.long_term_path.read_text()
            assert f"### Compacted: {old_date.isoformat()}" in lt_content
            assert "- Alpha fact" in lt_content

    def test_compact_callback_receives_stripped_entries(self):
        """Callback should receive prefix-stripped, deduped entries."""
        with tempfile.TemporaryDirectory() as td:
            received_entries: list[list[str]] = []

            def capture_summarize(entries: list[str]) -> str:
                received_entries.append(list(entries))
                return "- summary\n"

            mm = self._make_manager(td, compact_summarize_fn=capture_summarize)
            old_date = datetime.now(UTC).date() - timedelta(days=10)
            path = mm.daily_path(old_date)
            path.write_text(
                f"# Memory — {old_date.isoformat()}\n\n"
                f"- [10:00] **general**: Fact one\n"
                f"- [11:00] **general**: Fact one\n"  # duplicate
                f"- [12:00] **preference**: Fact two\n"
            )
            mm.compact_daily_files(days_to_keep=7, summarize=True)

            assert len(received_entries) == 1
            entries = received_entries[0]
            # Should be stripped of prefix
            assert "Fact one" in entries
            assert "Fact two" in entries
            # Should be deduped
            assert entries.count("Fact one") == 1
            # Should NOT contain raw prefix
            assert not any(e.startswith("- [") for e in entries)

    def test_compact_llm_returns_whitespace_falls_back(self):
        """Whitespace-only LLM response should fall back to extractive."""
        with tempfile.TemporaryDirectory() as td:

            def whitespace_summarize(entries: list[str]) -> str:
                return "   \n"

            mm = self._make_manager(td, compact_summarize_fn=whitespace_summarize)
            old_date = self._create_old_file(mm)
            mm.compact_daily_files(days_to_keep=7, summarize=True)

            lt_content = mm.long_term_path.read_text()
            assert f"### Compacted: {old_date.isoformat()}" in lt_content
            assert "- Alpha fact" in lt_content

    def test_compact_llm_returns_none_falls_back(self):
        """Callback returning None should fall back to extractive."""
        with tempfile.TemporaryDirectory() as td:

            def none_summarize(entries: list[str]) -> str:
                return None  # type: ignore[return-value]

            mm = self._make_manager(td, compact_summarize_fn=none_summarize)
            old_date = self._create_old_file(mm)
            mm.compact_daily_files(days_to_keep=7, summarize=True)

            lt_content = mm.long_term_path.read_text()
            assert f"### Compacted: {old_date.isoformat()}" in lt_content
            assert "- Alpha fact" in lt_content

    def test_compact_multiple_files_hits_safety_limit(self):
        """Incremental lt_lines tracking should trigger safety limit mid-compaction."""
        with tempfile.TemporaryDirectory() as td:
            # max_long_term_lines=3 → safety limit at 2x = 6 lines.
            # File 1 compacts: creates MEMORY.md header (2 lines) then appends
            # blank+section_header+5 entries (7 lines) → 9 total. 9 >= 6, so
            # file 2 should be kept.
            mm = self._make_manager(td, max_long_term_lines=3)

            dates = []
            for days_ago in [12, 11]:
                old_date = datetime.now(UTC).date() - timedelta(days=days_ago)
                path = mm.daily_path(old_date)
                path.write_text(
                    f"# Memory — {old_date.isoformat()}\n\n"
                    + "".join(f"- [10:0{i}] **general**: Entry {i}\n" for i in range(5))
                )
                dates.append((old_date, path))

            result = mm.compact_daily_files(days_to_keep=7, summarize=True)
            # First file should compact; second should be kept (safety limit)
            assert "Compacted 1 file(s)" in result
            assert "Warning" in result
            assert "kept" in result
            # The second file should still exist on disk
            assert dates[1][1].exists()


class TestWorkspaceConfigValidation:
    def test_recency_half_life_rejects_zero(self):
        with pytest.raises(ValidationError, match="recency_half_life_days"):
            WorkspaceConfig(recency_half_life_days=0.0)

    def test_recency_half_life_rejects_negative(self):
        with pytest.raises(ValidationError, match="recency_half_life_days"):
            WorkspaceConfig(recency_half_life_days=-5.0)

    def test_recency_half_life_accepts_positive(self):
        cfg = WorkspaceConfig(recency_half_life_days=7.0)
        assert cfg.recency_half_life_days == 7.0

    def test_compact_max_tokens_rejects_zero(self):
        with pytest.raises(ValidationError, match="compact_max_tokens"):
            WorkspaceConfig(compact_max_tokens=0)

    def test_compact_max_tokens_rejects_negative(self):
        with pytest.raises(ValidationError, match="compact_max_tokens"):
            WorkspaceConfig(compact_max_tokens=-1)
