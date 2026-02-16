"""Tests for the memory system."""

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from taskrunner.memory import MemoryManager


class TestMemoryManager:
    def _make_manager(self, td: str) -> MemoryManager:
        return MemoryManager(workspace_dir=td, timezone_name="UTC")

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
            entry_line = next(i for i, l in enumerate(lines, 1) if "Delete me" in l)
            today_str = datetime.now(timezone.utc).date().isoformat()
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
            today_str = datetime.now(timezone.utc).date().isoformat()
            result = mm.delete_memory(today_str, 999)
            assert "out of range" in result

    def test_delete_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.update_long_term("- Line to delete")
            # Find the line number
            lines = mm.long_term_path.read_text().splitlines()
            entry_line = next(i for i, l in enumerate(lines, 1) if "Line to delete" in l)
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
            entry_line = next(i for i, l in enumerate(lines, 1) if "Old text" in l)
            today_str = datetime.now(timezone.utc).date().isoformat()
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
            today_str = datetime.now(timezone.utc).date().isoformat()
            result = mm.edit_memory(today_str, 999, "replacement")
            assert "out of range" in result

    def test_edit_long_term(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.update_long_term("- Old fact")
            lines = mm.long_term_path.read_text().splitlines()
            entry_line = next(i for i, l in enumerate(lines, 1) if "Old fact" in l)
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
            old_date = (datetime.now(timezone.utc).date() - timedelta(days=10))
            old_path = mm.daily_path(old_date)
            old_path.write_text(f"# Memory — {old_date.isoformat()}\n\n- [10:00] **general**: Old note\n")
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
            old_date = (datetime.now(timezone.utc).date() - timedelta(days=10))
            old_path = mm.daily_path(old_date)
            old_path.write_text(f"# Memory — {old_date.isoformat()}\n\n- [10:00] **general**: Note 1\n- [11:00] **general**: Note 2\n")
            mm.compact_daily_files(days_to_keep=7)
            lt_content = mm.long_term_path.read_text()
            assert "2 entries (compacted)" in lt_content

    def test_compact_deletes_empty_files(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            old_date = (datetime.now(timezone.utc).date() - timedelta(days=10))
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
