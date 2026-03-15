"""Tests for hot-reload config support."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from creel.config_reload import (
    NON_RELOADABLE_FIELDS,
    RELOADABLE_FIELDS,
    ConfigChange,
    ReloadResult,
    classify_changes,
    diff_configs,
    reload_from_path,
)
from creel.daemon.watcher import ConfigWatcher  # noqa: I001
from creel.models import (
    AgentConfig,
    AgentDefinition,
    ChannelsConfig,
    LLMConfig,
    SessionConfig,
    WorkspaceConfig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent_def(**overrides) -> AgentDefinition:
    defaults = dict(
        system_prompt="You are a helpful assistant.",
        llm=LLMConfig(model="claude-sonnet-4-6", max_tokens=100),
        agent=AgentConfig(max_turns=5),
        session=SessionConfig(sessions_dir="/tmp/test-sessions", max_history=50),
        workspace=WorkspaceConfig(path="/tmp/test-workspace"),
        channels=ChannelsConfig(),
    )
    defaults.update(overrides)
    return AgentDefinition(**defaults)


@pytest.fixture
def config_a() -> AgentDefinition:
    return _make_agent_def()


@pytest.fixture
def config_b() -> AgentDefinition:
    return _make_agent_def(
        system_prompt="You are a different assistant.",
        llm=LLMConfig(model="claude-haiku-4-5", max_tokens=200),
    )


def _write_config(path: Path, agent_def: AgentDefinition) -> None:
    data = agent_def.model_dump()
    path.write_text(yaml.dump(data, default_flow_style=False))


# ---------------------------------------------------------------------------
# diff_configs
# ---------------------------------------------------------------------------


class TestDiffConfigs:
    def test_identical_configs_no_changes(self, config_a):
        changes = diff_configs(config_a, config_a)
        assert changes == []

    def test_system_prompt_change(self, config_a, config_b):
        changes = diff_configs(config_a, config_b)
        field_names = {c.field for c in changes}
        assert "system_prompt" in field_names

    def test_llm_model_change(self, config_a, config_b):
        changes = diff_configs(config_a, config_b)
        field_names = {c.field for c in changes}
        assert "llm" in field_names

    def test_change_contains_old_and_new(self, config_a, config_b):
        changes = diff_configs(config_a, config_b)
        prompt_change = next(c for c in changes if c.field == "system_prompt")
        assert prompt_change.old_value == "You are a helpful assistant."
        assert prompt_change.new_value == "You are a different assistant."


# ---------------------------------------------------------------------------
# classify_changes
# ---------------------------------------------------------------------------


class TestClassifyChanges:
    def test_reloadable_change(self):
        change = ConfigChange(field="llm", old_value={"model": "a"}, new_value={"model": "b"})
        reloadable, non_reloadable = classify_changes([change])
        assert len(reloadable) == 1
        assert len(non_reloadable) == 0

    def test_non_reloadable_sessions_dir(self):
        change = ConfigChange(
            field="session",
            old_value={"sessions_dir": "/old", "max_history": 50},
            new_value={"sessions_dir": "/new", "max_history": 50},
        )
        reloadable, non_reloadable = classify_changes([change])
        assert len(non_reloadable) == 1
        assert non_reloadable[0].field == "session.sessions_dir"

    def test_session_max_history_is_reloadable(self):
        change = ConfigChange(
            field="session",
            old_value={"sessions_dir": "/same", "max_history": 50},
            new_value={"sessions_dir": "/same", "max_history": 100},
        )
        reloadable, non_reloadable = classify_changes([change])
        assert len(reloadable) == 1
        assert len(non_reloadable) == 0

    def test_mixed_changes(self):
        changes = [
            ConfigChange(field="llm", old_value={}, new_value={"model": "new"}),
            ConfigChange(
                field="session",
                old_value={"sessions_dir": "/old"},
                new_value={"sessions_dir": "/new"},
            ),
        ]
        reloadable, non_reloadable = classify_changes(changes)
        assert len(reloadable) == 1
        assert reloadable[0].field == "llm"
        assert len(non_reloadable) == 1


# ---------------------------------------------------------------------------
# reload_from_path
# ---------------------------------------------------------------------------


class TestReloadFromPath:
    def test_file_not_found(self, config_a):
        result = reload_from_path("/nonexistent/agent.yaml", config_a)
        assert not result.success
        assert "not found" in result.error

    def test_invalid_yaml(self, tmp_path, config_a):
        bad_file = tmp_path / "agent.yaml"
        bad_file.write_text(": :\n  invalid: [unterminated")
        result = reload_from_path(bad_file, config_a)
        assert not result.success

    def test_no_changes(self, tmp_path, config_a):
        config_file = tmp_path / "agent.yaml"
        _write_config(config_file, config_a)
        result = reload_from_path(config_file, config_a)
        assert result.success
        assert result.changed_count == 0

    def test_detects_changes(self, tmp_path, config_a, config_b):
        config_file = tmp_path / "agent.yaml"
        _write_config(config_file, config_b)
        result = reload_from_path(config_file, config_a)
        assert result.success
        assert result.changed_count > 0

    def test_invalid_config_values(self, tmp_path, config_a):
        config_file = tmp_path / "agent.yaml"
        config_file.write_text(yaml.dump({"not_a_field": True}))
        result = reload_from_path(config_file, config_a)
        assert not result.success


# ---------------------------------------------------------------------------
# ReloadResult
# ---------------------------------------------------------------------------


class TestReloadResult:
    def test_summary_no_changes(self):
        result = ReloadResult(success=True)
        assert "No config changes" in result.summary()

    def test_summary_with_changes(self):
        result = ReloadResult(
            success=True,
            changes=[ConfigChange(field="llm"), ConfigChange(field="tools")],
        )
        assert "2 setting(s) updated" in result.summary()

    def test_summary_with_error(self):
        result = ReloadResult(success=False, error="bad yaml")
        assert "bad yaml" in result.summary()

    def test_summary_with_non_reloadable(self):
        result = ReloadResult(
            success=True,
            non_reloadable=[ConfigChange(field="session.sessions_dir")],
        )
        assert "non-reloadable" in result.summary()

    def test_changed_count(self):
        result = ReloadResult(
            success=True,
            changes=[ConfigChange(field="llm"), ConfigChange(field="agent")],
        )
        assert result.changed_count == 2


# ---------------------------------------------------------------------------
# ConfigChange.__str__
# ---------------------------------------------------------------------------


class TestConfigChange:
    def test_str_representation(self):
        change = ConfigChange(field="llm", old_value="a", new_value="b")
        s = str(change)
        assert "llm" in s
        assert '"a"' in s
        assert '"b"' in s

    def test_str_long_string(self):
        change = ConfigChange(field="system_prompt", old_value="x" * 100, new_value="y")
        s = str(change)
        assert "..." in s

    def test_str_dict_value(self):
        change = ConfigChange(field="tools", old_value={"a": 1, "b": 2}, new_value={})
        s = str(change)
        assert "{2 items}" in s

    def test_str_none_value(self):
        change = ConfigChange(field="guardian", old_value=None, new_value={"enabled": True})
        s = str(change)
        assert "null" in s


# ---------------------------------------------------------------------------
# DaemonService.reload_config
# ---------------------------------------------------------------------------


class TestServiceReload:
    def test_reload_config_no_path(self, minimal_agent_def, tmp_path):
        from creel.daemon.service import DaemonService

        server = MagicMock()
        server._session_mgr = MagicMock()
        server._guardian = None
        svc = DaemonService(minimal_agent_def, server=server)
        result = svc.reload_config()
        assert not result.success
        assert "No config path" in result.error

    def test_reload_config_no_changes(self, minimal_agent_def, tmp_path):
        from creel.daemon.service import DaemonService

        config_file = tmp_path / "agent.yaml"
        _write_config(config_file, minimal_agent_def)

        server = MagicMock()
        server._session_mgr = MagicMock()
        server._guardian = None
        svc = DaemonService(minimal_agent_def, server=server, config_path=config_file)
        result = svc.reload_config()
        assert result.success
        assert result.changed_count == 0

    def test_reload_config_applies_changes(self, minimal_agent_def, tmp_path):
        from creel.daemon.service import DaemonService

        config_file = tmp_path / "agent.yaml"
        new_def = _make_agent_def(
            system_prompt="Updated prompt",
            session=SessionConfig(
                sessions_dir=minimal_agent_def.session.sessions_dir,
                max_history=50,
            ),
        )
        _write_config(config_file, new_def)

        server = MagicMock()
        server._session_mgr = MagicMock()
        server._guardian = None
        svc = DaemonService(minimal_agent_def, server=server, config_path=config_file)
        result = svc.reload_config()

        assert result.success
        assert result.changed_count > 0
        assert svc._agent_def.system_prompt == "Updated prompt"
        # ChatServer should also have the updated config via update_agent_def
        server.update_agent_def.assert_called_once()

    def test_reload_config_invalid_file(self, minimal_agent_def, tmp_path):
        from creel.daemon.service import DaemonService

        config_file = tmp_path / "agent.yaml"
        config_file.write_text("invalid: [yaml: broken")

        server = MagicMock()
        server._session_mgr = MagicMock()
        server._guardian = None
        svc = DaemonService(minimal_agent_def, server=server, config_path=config_file)
        result = svc.reload_config()
        assert not result.success
        # Original config should be preserved
        assert svc._agent_def.system_prompt == "You are a helpful assistant."

    def test_reload_config_with_explicit_path(self, minimal_agent_def, tmp_path):
        from creel.daemon.service import DaemonService

        config_file = tmp_path / "other.yaml"
        new_def = _make_agent_def(
            system_prompt="From other path",
            session=SessionConfig(
                sessions_dir=minimal_agent_def.session.sessions_dir,
                max_history=50,
            ),
        )
        _write_config(config_file, new_def)

        server = MagicMock()
        server._session_mgr = MagicMock()
        server._guardian = None
        svc = DaemonService(minimal_agent_def, server=server)
        result = svc.reload_config(config_path=config_file)

        assert result.success
        assert svc._agent_def.system_prompt == "From other path"

    def test_concurrent_reloads_are_serialized(self, minimal_agent_def, tmp_path):
        """Only one reload should run at a time; concurrent attempts are skipped."""
        from creel.daemon.service import DaemonService

        config_file = tmp_path / "agent.yaml"
        new_def = _make_agent_def(
            system_prompt="Concurrent test",
            session=SessionConfig(
                sessions_dir=minimal_agent_def.session.sessions_dir,
                max_history=50,
            ),
        )
        _write_config(config_file, new_def)

        server = MagicMock()
        server._session_mgr = MagicMock()
        server._guardian = None
        svc = DaemonService(minimal_agent_def, server=server, config_path=config_file)

        # Hold the reload lock so any concurrent reload_config() call is skipped.
        svc._reload_lock.acquire()
        try:
            result = svc.reload_config()
        finally:
            svc._reload_lock.release()

        # Should succeed but with no changes (skipped due to lock)
        assert result.success
        assert result.changed_count == 0
        # Original config should be preserved
        assert svc._agent_def.system_prompt == "You are a helpful assistant."


# ---------------------------------------------------------------------------
# ConfigWatcher
# ---------------------------------------------------------------------------


class TestConfigWatcher:
    def test_detects_file_change(self, tmp_path):
        config_file = tmp_path / "agent.yaml"
        config_file.write_text("initial")

        triggered = threading.Event()

        def on_change():
            triggered.set()

        watcher = ConfigWatcher(config_file, on_change, poll_interval=0.1)
        watcher.start()
        assert watcher.running

        try:
            # Bump mtime into the future to guarantee the watcher sees a change
            # without relying on wall-clock sleeps.
            future = time.time() + 10
            config_file.write_text("modified")
            os.utime(config_file, (future, future))

            assert triggered.wait(timeout=2.0), "Watcher did not detect file change"
        finally:
            watcher.stop()
            assert not watcher.running

    def test_no_trigger_on_startup(self, tmp_path):
        config_file = tmp_path / "agent.yaml"
        config_file.write_text("initial")

        triggered = threading.Event()
        watcher = ConfigWatcher(config_file, lambda: triggered.set(), poll_interval=0.1)
        watcher.start()

        try:
            time.sleep(0.3)
            assert not triggered.is_set(), "Watcher should not fire on startup"
        finally:
            watcher.stop()

    def test_stop_is_idempotent(self, tmp_path):
        config_file = tmp_path / "agent.yaml"
        config_file.write_text("data")

        watcher = ConfigWatcher(config_file, lambda: None, poll_interval=0.1)
        watcher.start()
        watcher.stop()
        watcher.stop()  # second stop should not raise

    def test_handles_missing_file(self, tmp_path):
        config_file = tmp_path / "nonexistent.yaml"
        triggered = threading.Event()
        watcher = ConfigWatcher(config_file, lambda: triggered.set(), poll_interval=0.1)
        watcher.start()

        try:
            time.sleep(0.3)
            assert not triggered.is_set()
        finally:
            watcher.stop()

    def test_callback_exception_does_not_crash(self, tmp_path):
        config_file = tmp_path / "agent.yaml"
        config_file.write_text("initial")

        call_count = 0

        def bad_callback():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("callback error")

        watcher = ConfigWatcher(config_file, bad_callback, poll_interval=0.1)
        watcher.start()

        try:
            future = time.time() + 10
            config_file.write_text("modified")
            os.utime(config_file, (future, future))
            time.sleep(0.5)
            assert call_count >= 1, "Callback should have been called despite error"
        finally:
            watcher.stop()


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    def test_non_reloadable_fields_are_valid(self):
        """Each non-reloadable field should be a dotted path starting with a reloadable top-level."""
        for field in NON_RELOADABLE_FIELDS:
            top = field.split(".")[0]
            assert top in RELOADABLE_FIELDS, f"{field} has unknown top-level: {top}"

    def test_reloadable_fields_match_model(self):
        """All reloadable fields should exist in AgentDefinition."""
        model_fields = set(AgentDefinition.model_fields.keys())
        for field in RELOADABLE_FIELDS:
            assert field in model_fields, f"{field} not in AgentDefinition"
