"""Tests for built-in monitor templates."""

from __future__ import annotations

import pytest

from creel.cron.models import Delivery
from creel.monitors.models import AlertLevel, Monitor
from creel.monitors.templates import (
    create_from_template,
    get_template,
    list_templates,
)


class TestListTemplates:
    def test_at_least_three_templates(self) -> None:
        templates = list_templates()
        assert len(templates) >= 3

    def test_known_templates_present(self) -> None:
        templates = list_templates()
        assert "urgent_email" in templates
        assert "calendar_conflicts" in templates
        assert "system_health" in templates


class TestGetTemplate:
    def test_get_existing(self) -> None:
        tmpl = get_template("urgent_email")
        assert tmpl is not None
        assert tmpl["executor"] == "gmail_readonly"

    def test_get_nonexistent(self) -> None:
        assert get_template("does_not_exist") is None


class TestCreateFromTemplate:
    def test_create_urgent_email(self) -> None:
        mon = create_from_template("urgent_email")
        assert isinstance(mon, Monitor)
        assert mon.name == "Urgent Email Monitor"
        assert mon.executor == "gmail_readonly"
        assert mon.alert_level == AlertLevel.URGENT
        assert mon.quiet_hours is not None
        assert mon.delivery.mode == "none"

    def test_create_calendar_conflicts(self) -> None:
        mon = create_from_template("calendar_conflicts")
        assert mon.executor == "gcal"
        assert mon.alert_level == AlertLevel.NOTICE

    def test_create_system_health(self) -> None:
        mon = create_from_template("system_health")
        assert mon.executor == "exec"
        assert mon.quiet_hours is None

    def test_create_with_delivery_override(self) -> None:
        delivery = Delivery(mode="announce", channel="telegram")
        mon = create_from_template("urgent_email", delivery=delivery)
        assert mon.delivery.mode == "announce"
        assert mon.delivery.channel == "telegram"

    def test_create_with_timezone_override(self) -> None:
        mon = create_from_template("urgent_email", quiet_hours_tz="America/Denver")
        assert mon.quiet_hours is not None
        assert mon.quiet_hours.timezone == "America/Denver"

    def test_create_unknown_template_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown template"):
            create_from_template("nonexistent")

    def test_each_template_creates_valid_monitor(self) -> None:
        for name in list_templates():
            mon = create_from_template(name)
            assert isinstance(mon, Monitor)
            assert mon.name
            assert mon.executor
            assert mon.prompt
