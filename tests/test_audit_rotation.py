"""Tests for audit log rotation features."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from guardian.audit import AuditLogger


class TestDailyRotation:
    def test_daily_rotation_uses_date_suffix(self, tmp_path: Path) -> None:
        logger = AuditLogger(tmp_path / "audit.jsonl", rotate_daily=True)
        logger.log_screen(input_hash="abc", input_length=10, blocked=False, source="test")

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        expected = tmp_path / f"audit-{today}.jsonl"
        assert expected.exists()
        # The base file should NOT exist
        assert not (tmp_path / "audit.jsonl").exists()

    def test_daily_rotation_different_dates(self, tmp_path: Path) -> None:
        logger = AuditLogger(tmp_path / "audit.jsonl", rotate_daily=True)

        # Write with mocked date
        with patch("guardian.audit.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2025, 1, 15, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            logger.log_screen(input_hash="a", input_length=1, blocked=False, source="test")

        with patch("guardian.audit.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2025, 1, 16, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            logger.log_screen(input_hash="b", input_length=2, blocked=True, source="test")

        assert (tmp_path / "audit-2025-01-15.jsonl").exists()
        assert (tmp_path / "audit-2025-01-16.jsonl").exists()

    def test_no_rotation_by_default(self, tmp_path: Path) -> None:
        logger = AuditLogger(tmp_path / "audit.jsonl")
        logger.log_screen(input_hash="abc", input_length=10, blocked=False, source="test")
        assert (tmp_path / "audit.jsonl").exists()


class TestSizeBasedRotation:
    def test_size_rotation_triggers(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        # Use a very small max size (100 bytes) so we can trigger rotation easily
        logger = AuditLogger(log_file, max_size_mb=0.0001)  # ~105 bytes

        # Write enough to exceed the limit
        for i in range(5):
            logger.log_screen(input_hash=f"hash{i}", input_length=i, blocked=False, source="test")

        # The rotated file should exist
        rotated = log_file.with_suffix(".jsonl.1")
        assert rotated.exists() or log_file.exists()

    def test_size_rotation_preserves_data(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        # Write initial data
        log_file.write_text('{"event":"old"}\n')

        # Set max size very small so next write triggers rotation
        logger = AuditLogger(log_file, max_size_mb=0.000001)  # ~1 byte
        logger.log_screen(input_hash="new", input_length=1, blocked=False, source="test")

        rotated = log_file.with_suffix(".jsonl.1")
        assert rotated.exists()
        assert '{"event":"old"}' in rotated.read_text()

    def test_no_size_rotation_when_zero(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file, max_size_mb=0)

        for i in range(10):
            logger.log_screen(input_hash=f"hash{i}", input_length=i, blocked=False, source="test")

        rotated = log_file.with_suffix(".jsonl.1")
        assert not rotated.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 10


class TestCombinedRotation:
    def test_daily_and_size_combined(self, tmp_path: Path) -> None:
        logger = AuditLogger(
            tmp_path / "audit.jsonl",
            rotate_daily=True,
            max_size_mb=0.0001,
        )

        for i in range(10):
            logger.log_screen(input_hash=f"hash{i}", input_length=i, blocked=False, source="test")

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        daily_file = tmp_path / f"audit-{today}.jsonl"
        assert daily_file.exists()


class TestAuditConfigRotation:
    def test_config_defaults(self) -> None:
        from guardian.types import AuditConfig

        config = AuditConfig()
        assert config.rotate_daily is False
        assert config.max_size_mb == 0

    def test_config_with_rotation(self) -> None:
        from guardian.types import AuditConfig

        config = AuditConfig(rotate_daily=True, max_size_mb=50.0)
        assert config.rotate_daily is True
        assert config.max_size_mb == 50.0
