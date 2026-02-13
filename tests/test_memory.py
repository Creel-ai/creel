"""Tests for the memory system."""

import tempfile
from datetime import date
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
            # Check file exists
            today = date.today()
            path = Path(td) / "memory" / f"{today.isoformat()}.md"
            assert path.exists()
            content = path.read_text()
            assert "coffee" in content
            assert "preference" in content

    def test_remember_appends(self):
        with tempfile.TemporaryDirectory() as td:
            mm = self._make_manager(td)
            mm.remember("First note")
            mm.remember("Second note")
            today = date.today()
            path = Path(td) / "memory" / f"{today.isoformat()}.md"
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
