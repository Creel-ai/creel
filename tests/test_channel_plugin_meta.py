"""Tests for channel plugin metadata and capabilities."""

from __future__ import annotations

from pydantic import BaseModel

from taskrunner.channels.plugin import ChannelCapability, ChannelPluginMeta


class _DummyConfig(BaseModel):
    host: str = "localhost"


def test_capability_flags_combine():
    caps = ChannelCapability.POLLING | ChannelCapability.SEND
    assert ChannelCapability.POLLING in caps
    assert ChannelCapability.SEND in caps
    assert ChannelCapability.WEBHOOK not in caps


def test_capability_all_members():
    expected = {
        "POLLING", "WEBHOOK", "SEND", "MEDIA", "REACTIONS",
        "READ_RECEIPTS", "TYPING_INDICATOR", "GROUP_CHAT", "WAIT_FOR_REPLY",
    }
    actual = {c.name for c in ChannelCapability}
    assert actual == expected


def test_plugin_meta_frozen():
    meta = ChannelPluginMeta(
        id="test",
        label="Test Channel",
        capabilities=ChannelCapability.SEND,
    )
    assert meta.id == "test"
    assert meta.label == "Test Channel"

    import pytest
    with pytest.raises(AttributeError):
        meta.id = "changed"  # type: ignore[misc]


def test_plugin_meta_defaults():
    meta = ChannelPluginMeta(
        id="x",
        label="X",
        capabilities=ChannelCapability.POLLING,
    )
    assert meta.config_schema is None
    assert meta.priority == 100
    assert meta.platform is None
    assert meta.extras == []


def test_plugin_meta_with_config_schema():
    meta = ChannelPluginMeta(
        id="test",
        label="Test",
        capabilities=ChannelCapability.SEND,
        config_schema=_DummyConfig,
    )
    assert meta.config_schema is _DummyConfig


def test_plugin_meta_platform_constraint():
    meta = ChannelPluginMeta(
        id="mac_only",
        label="Mac Only",
        capabilities=ChannelCapability.POLLING,
        platform="darwin",
    )
    assert meta.platform == "darwin"


def test_plugin_meta_extras():
    meta = ChannelPluginMeta(
        id="wa",
        label="WhatsApp",
        capabilities=ChannelCapability.SEND,
        extras=["whatsapp"],
    )
    assert meta.extras == ["whatsapp"]
