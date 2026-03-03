"""Tests for channel plugin registry."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from creel.channels.base import Channel
from creel.channels.plugin import ChannelCapability, ChannelPluginMeta
from creel.channels.registry import ChannelRegistry


class _DummyChannel(Channel):
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def listen(self, callback):
        pass

    def send(self, recipient, text):
        pass


def _make_meta(id: str, priority: int = 100, platform: str | None = None):
    return ChannelPluginMeta(
        id=id,
        label=id.title(),
        capabilities=ChannelCapability.SEND,
        priority=priority,
        platform=platform,
    )


def _make_factory(**defaults):
    def factory(config):
        return _DummyChannel(**{**defaults, **config})

    return factory


class TestRegistration:
    def test_register_and_get(self):
        reg = ChannelRegistry()
        meta = _make_meta("test")
        reg.register(meta, _make_factory())

        entry = reg.get("test")
        assert entry is not None
        assert entry.meta.id == "test"

    def test_get_unknown_returns_none(self):
        reg = ChannelRegistry()
        assert reg.get("nonexistent") is None

    def test_overwrite_warns(self, caplog):
        reg = ChannelRegistry()
        meta = _make_meta("dup")
        reg.register(meta, _make_factory())
        reg.register(meta, _make_factory())
        assert "Overwriting" in caplog.text


class TestAvailable:
    def test_filters_by_platform(self):
        reg = ChannelRegistry()
        reg.register(_make_meta("any"), _make_factory())
        reg.register(_make_meta("darwin_only", platform="darwin"), _make_factory())
        reg.register(_make_meta("linux_only", platform="linux"), _make_factory())

        available = reg.available()
        ids = {m.id for m in available}

        # "any" is always available, platform-specific depends on sys.platform
        assert "any" in ids
        if sys.platform == "darwin":
            assert "darwin_only" in ids
            assert "linux_only" not in ids
        elif sys.platform == "linux":
            assert "linux_only" in ids
            assert "darwin_only" not in ids

    def test_sorted_by_priority_then_id(self):
        reg = ChannelRegistry()
        reg.register(_make_meta("beta", priority=50), _make_factory())
        reg.register(_make_meta("alpha", priority=50), _make_factory())
        reg.register(_make_meta("first", priority=10), _make_factory())

        available = reg.available()
        ids = [m.id for m in available]
        assert ids == ["first", "alpha", "beta"]


class TestCreateChannel:
    def test_creates_from_factory(self):
        reg = ChannelRegistry()
        reg.register(_make_meta("test"), _make_factory(default_val="x"))

        ch = reg.create_channel("test", {"key": "value"})
        assert isinstance(ch, _DummyChannel)
        assert ch.kwargs["key"] == "value"
        assert ch.kwargs["default_val"] == "x"

    def test_unknown_channel_raises(self):
        reg = ChannelRegistry()
        with pytest.raises(ValueError, match="Unknown channel 'nope'"):
            reg.create_channel("nope", {})


class TestDiscover:
    def test_discover_loads_entry_points(self):
        mock_meta = _make_meta("discovered")
        mock_factory = _make_factory()

        def mock_register_fn():
            return mock_meta, mock_factory

        mock_ep = MagicMock()
        mock_ep.name = "discovered"
        mock_ep.load.return_value = mock_register_fn

        mock_eps = MagicMock()
        mock_eps.select.return_value = [mock_ep]

        with patch("importlib.metadata.entry_points", return_value=mock_eps):
            reg = ChannelRegistry()
            reg.discover()

        entry = reg.get("discovered")
        assert entry is not None
        assert entry.meta.id == "discovered"

    def test_discover_handles_bad_plugin(self, caplog):
        import logging

        mock_ep = MagicMock()
        mock_ep.name = "broken"
        mock_ep.load.side_effect = ImportError("no such module")

        mock_eps = MagicMock()
        mock_eps.select.return_value = [mock_ep]

        with caplog.at_level(logging.DEBUG, logger="creel.channels.registry"):
            with patch("importlib.metadata.entry_points", return_value=mock_eps):
                reg = ChannelRegistry()
                reg.discover()

        assert reg.get("broken") is None
        assert "Failed to load" in caplog.text
