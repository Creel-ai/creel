"""Tests for quiet hours functionality."""

import pytest
from datetime import datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from creel.models import QuietHoursConfig
from creel.quiet_hours import is_quiet_hours, should_suppress


class TestQuietHours:
    """Test quiet hours functionality."""

    def test_disabled_config(self):
        """Test that disabled config never triggers quiet hours."""
        config = QuietHoursConfig(enabled=False)
        
        # Should never be quiet hours when disabled
        with patch('creel.quiet_hours.datetime') as mock_dt:
            # Test during what would be quiet hours
            mock_dt.now.return_value = datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("UTC"))  # 01:00 UTC
            assert is_quiet_hours(config) is False
            assert should_suppress(config) is False

    def test_within_quiet_hours_same_day(self):
        """Test detection when quiet hours are within the same day."""
        config = QuietHoursConfig(
            enabled=True,
            start="09:00",
            end="17:00",
            timezone="UTC"
        )
        
        with patch('creel.quiet_hours.datetime') as mock_dt:
            # Test within quiet hours
            mock_dt.now.return_value = datetime(2024, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC"))  # 12:00 UTC
            assert is_quiet_hours(config) is True
            assert should_suppress(config) is True
            
            # Test at start boundary (inclusive)
            mock_dt.now.return_value = datetime(2024, 1, 1, 9, 0, tzinfo=ZoneInfo("UTC"))  # 09:00 UTC
            assert is_quiet_hours(config) is True
            
            # Test at end boundary (exclusive)
            mock_dt.now.return_value = datetime(2024, 1, 1, 17, 0, tzinfo=ZoneInfo("UTC"))  # 17:00 UTC
            assert is_quiet_hours(config) is False

    def test_outside_quiet_hours_same_day(self):
        """Test detection when outside quiet hours within the same day."""
        config = QuietHoursConfig(
            enabled=True,
            start="09:00",
            end="17:00",
            timezone="UTC"
        )
        
        with patch('creel.quiet_hours.datetime') as mock_dt:
            # Test before quiet hours
            mock_dt.now.return_value = datetime(2024, 1, 1, 8, 0, tzinfo=ZoneInfo("UTC"))  # 08:00 UTC
            assert is_quiet_hours(config) is False
            assert should_suppress(config) is False
            
            # Test after quiet hours
            mock_dt.now.return_value = datetime(2024, 1, 1, 18, 0, tzinfo=ZoneInfo("UTC"))  # 18:00 UTC
            assert is_quiet_hours(config) is False
            assert should_suppress(config) is False

    def test_midnight_crossing_within_quiet_hours(self):
        """Test overnight ranges (23:00 → 08:00 crossing midnight)."""
        config = QuietHoursConfig(
            enabled=True,
            start="23:00",
            end="08:00",
            timezone="UTC"
        )
        
        with patch('creel.quiet_hours.datetime') as mock_dt:
            # Test late night (after start)
            mock_dt.now.return_value = datetime(2024, 1, 1, 23, 30, tzinfo=ZoneInfo("UTC"))  # 23:30 UTC
            assert is_quiet_hours(config) is True
            assert should_suppress(config) is True
            
            # Test early morning (before end)
            mock_dt.now.return_value = datetime(2024, 1, 1, 7, 0, tzinfo=ZoneInfo("UTC"))  # 07:00 UTC
            assert is_quiet_hours(config) is True
            assert should_suppress(config) is True
            
            # Test at start boundary (inclusive)
            mock_dt.now.return_value = datetime(2024, 1, 1, 23, 0, tzinfo=ZoneInfo("UTC"))  # 23:00 UTC
            assert is_quiet_hours(config) is True
            
            # Test at end boundary (exclusive)
            mock_dt.now.return_value = datetime(2024, 1, 1, 8, 0, tzinfo=ZoneInfo("UTC"))  # 08:00 UTC
            assert is_quiet_hours(config) is False

    def test_midnight_crossing_outside_quiet_hours(self):
        """Test times outside overnight ranges."""
        config = QuietHoursConfig(
            enabled=True,
            start="23:00",
            end="08:00",
            timezone="UTC"
        )
        
        with patch('creel.quiet_hours.datetime') as mock_dt:
            # Test middle of the day
            mock_dt.now.return_value = datetime(2024, 1, 1, 15, 0, tzinfo=ZoneInfo("UTC"))  # 15:00 UTC
            assert is_quiet_hours(config) is False
            assert should_suppress(config) is False
            
            # Test evening before quiet hours
            mock_dt.now.return_value = datetime(2024, 1, 1, 22, 0, tzinfo=ZoneInfo("UTC"))  # 22:00 UTC
            assert is_quiet_hours(config) is False
            assert should_suppress(config) is False
            
            # Test morning after quiet hours
            mock_dt.now.return_value = datetime(2024, 1, 1, 9, 0, tzinfo=ZoneInfo("UTC"))  # 09:00 UTC
            assert is_quiet_hours(config) is False
            assert should_suppress(config) is False

    def test_urgent_bypass(self):
        """Test that urgent messages bypass quiet hours when allowed."""
        config = QuietHoursConfig(
            enabled=True,
            start="23:00",
            end="08:00",
            timezone="UTC",
            allow_urgent=True
        )
        
        with patch('creel.quiet_hours.datetime') as mock_dt:
            # Set time within quiet hours
            mock_dt.now.return_value = datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("UTC"))  # 01:00 UTC
            
            # Normal message should be suppressed
            assert should_suppress(config, urgent=False) is True
            
            # Urgent message should not be suppressed
            assert should_suppress(config, urgent=True) is False

    def test_urgent_disabled(self):
        """Test that urgent messages are still suppressed when allow_urgent is False."""
        config = QuietHoursConfig(
            enabled=True,
            start="23:00",
            end="08:00",
            timezone="UTC",
            allow_urgent=False
        )
        
        with patch('creel.quiet_hours.datetime') as mock_dt:
            # Set time within quiet hours
            mock_dt.now.return_value = datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("UTC"))  # 01:00 UTC
            
            # Both normal and urgent messages should be suppressed
            assert should_suppress(config, urgent=False) is True
            assert should_suppress(config, urgent=True) is True

    def test_timezone_handling(self):
        """Test that timezone is correctly applied."""
        config = QuietHoursConfig(
            enabled=True,
            start="23:00",
            end="08:00",
            timezone="America/Denver"  # MST/MDT
        )
        
        with patch('creel.quiet_hours.datetime') as mock_dt:
            # Set time that would be outside quiet hours in UTC but inside in Denver
            # 06:00 UTC = 23:00 MST (during standard time)
            mock_dt.now.return_value = datetime(2024, 1, 1, 23, 0, tzinfo=ZoneInfo("America/Denver"))
            assert is_quiet_hours(config) is True

    def test_error_handling(self):
        """Test error handling for invalid time formats."""
        config = QuietHoursConfig(
            enabled=True,
            start="invalid-time",
            end="08:00",
            timezone="UTC"
        )
        
        # Should return False on error (fail safe)
        assert is_quiet_hours(config) is False
        assert should_suppress(config) is False

    def test_invalid_timezone(self):
        """Test error handling for invalid timezone."""
        config = QuietHoursConfig(
            enabled=True,
            start="23:00",
            end="08:00",
            timezone="Invalid/Timezone"
        )
        
        # Should return False on error (fail safe)
        assert is_quiet_hours(config) is False
        assert should_suppress(config) is False