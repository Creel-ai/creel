"""Tests for the SkillRegistry."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

from creel.skills.models import SkillMeta, ToolSpec
from creel.skills.registry import (
    _BUILTIN_TOOL_NAMES,
    SkillRegistry,
    _LazyExecute,
    get_shared_registry,
    reset_shared_registry,
)


def _dummy_execute(config):
    return "{}"


def _make_meta(skill_id: str, tool_names: list[str], **kwargs) -> SkillMeta:
    tools = tuple(ToolSpec(name=n, description=f"{n} desc") for n in tool_names)
    return SkillMeta(id=skill_id, label=skill_id.title(), tools=tools, **kwargs)


class TestRegister:
    def test_register_simple(self):
        reg = SkillRegistry()
        meta = _make_meta("weather", ["check_weather"])
        reg.register(meta, _dummy_execute)

        assert reg.get_skill("weather") is not None
        assert reg.get_skill("weather").meta is meta

    def test_register_multi_tool(self):
        reg = SkillRegistry()
        meta = _make_meta("gmail_modify", ["modify_email", "trash_email", "delete_email"])
        reg.register(meta, _dummy_execute)

        assert reg.skill_for_tool("modify_email") == "gmail_modify"
        assert reg.skill_for_tool("trash_email") == "gmail_modify"
        assert reg.skill_for_tool("delete_email") == "gmail_modify"

    def test_tool_name_collision_raises(self):
        reg = SkillRegistry()
        meta1 = _make_meta("skill_a", ["shared_tool"])
        meta2 = _make_meta("skill_b", ["shared_tool"])
        reg.register(meta1, _dummy_execute)

        with pytest.raises(ValueError, match="already registered by skill 'skill_a'"):
            reg.register(meta2, _dummy_execute)

    def test_re_register_same_skill(self):
        """Re-registering the same skill ID should overwrite cleanly."""
        reg = SkillRegistry()
        meta1 = _make_meta("weather", ["check_weather"])
        reg.register(meta1, _dummy_execute)

        meta2 = _make_meta("weather", ["get_weather"])
        reg.register(meta2, _dummy_execute)

        assert reg.skill_for_tool("get_weather") == "weather"
        assert reg.skill_for_tool("check_weather") is None  # old mapping removed


class TestGetTool:
    def test_get_tool_found(self):
        reg = SkillRegistry()
        meta = _make_meta("weather", ["check_weather"])
        reg.register(meta, _dummy_execute)

        result = reg.get_tool("check_weather")
        assert result is not None
        tool_spec, entry = result
        assert tool_spec.name == "check_weather"
        assert entry.meta.id == "weather"

    def test_get_tool_not_found(self):
        reg = SkillRegistry()
        assert reg.get_tool("nonexistent") is None


class TestAllSkills:
    def test_all_skills_sorted(self):
        reg = SkillRegistry()
        reg.register(_make_meta("weather", ["check_weather"]), _dummy_execute)
        reg.register(_make_meta("brave_search", ["web_search"]), _dummy_execute)

        skills = reg.all_skills()
        assert [s.id for s in skills] == ["brave_search", "weather"]

    def test_platform_filtering(self):
        reg = SkillRegistry()
        reg.register(_make_meta("cross_platform", ["tool_a"]), _dummy_execute)
        reg.register(_make_meta("darwin_only", ["tool_b"], platform="darwin"), _dummy_execute)
        reg.register(_make_meta("linux_only", ["tool_c"], platform="linux"), _dummy_execute)

        skills = reg.all_skills()
        skill_ids = [s.id for s in skills]
        # cross_platform always included; only the current platform's skill is included
        assert "cross_platform" in skill_ids
        # darwin_only or linux_only depending on test platform
        import sys

        if sys.platform == "darwin":
            assert "darwin_only" in skill_ids
            assert "linux_only" not in skill_ids
        elif sys.platform == "linux":
            assert "linux_only" in skill_ids
            assert "darwin_only" not in skill_ids


class TestAllToolNames:
    def test_all_tool_names(self):
        reg = SkillRegistry()
        reg.register(_make_meta("a", ["z_tool", "a_tool"]), _dummy_execute)
        assert reg.all_tool_names() == ["a_tool", "z_tool"]


class TestSkillForTool:
    def test_known(self):
        reg = SkillRegistry()
        reg.register(_make_meta("weather", ["check_weather"]), _dummy_execute)
        assert reg.skill_for_tool("check_weather") == "weather"

    def test_unknown(self):
        reg = SkillRegistry()
        assert reg.skill_for_tool("nonexistent") is None


# ---------------------------------------------------------------------------
# Built-in tool name shadowing
# ---------------------------------------------------------------------------


class TestBuiltinToolNameShadowing:
    """Registering a skill with a tool name matching a built-in must raise ValueError."""

    @pytest.mark.parametrize("builtin_name", sorted(_BUILTIN_TOOL_NAMES))
    def test_builtin_name_raises(self, builtin_name):
        reg = SkillRegistry()
        meta = _make_meta("evil_skill", [builtin_name])
        with pytest.raises(
            ValueError,
            match=f"Tool name '{builtin_name}' is a built-in tool",
        ):
            reg.register(meta, _dummy_execute)

    def test_builtin_shadowing_multi_tool_raises(self):
        """Even if only one of several tool names matches a built-in, it must raise."""
        reg = SkillRegistry()
        meta = _make_meta("sneaky", ["harmless_tool", "remember"])
        with pytest.raises(ValueError, match="built-in tool"):
            reg.register(meta, _dummy_execute)

    def test_non_builtin_name_succeeds(self):
        reg = SkillRegistry()
        meta = _make_meta("my_skill", ["definitely_not_builtin"])
        reg.register(meta, _dummy_execute)
        assert reg.get_skill("my_skill") is not None


# ---------------------------------------------------------------------------
# _LazyExecute
# ---------------------------------------------------------------------------


class TestLazyExecute:
    """Verify _LazyExecute defers the import until first call."""

    def test_deferred_import(self):
        """The executor module is not imported until __call__ is invoked."""
        lazy = _LazyExecute("executors.exec.executor")
        # Before calling — _real_execute should still be None
        assert lazy._real_execute is None

    def test_calls_real_execute_on_first_invocation(self):
        """First __call__ triggers import and delegates to the real execute function."""
        mock_meta = _make_meta("test_skill", ["tool_a"])
        mock_execute = MagicMock(return_value='{"ok": true}')

        mock_module = MagicMock()
        mock_module.register_skill.return_value = (mock_meta, mock_execute)

        lazy = _LazyExecute("some.fake.module")

        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            config = MagicMock()
            result = lazy(config)

            mock_import.assert_called_once_with("some.fake.module")
            mock_execute.assert_called_once_with(config)
            assert result == '{"ok": true}'

    def test_second_call_does_not_reimport(self):
        """Subsequent calls reuse the cached execute function."""
        mock_meta = _make_meta("test_skill", ["tool_a"])
        mock_execute = MagicMock(return_value="{}")

        mock_module = MagicMock()
        mock_module.register_skill.return_value = (mock_meta, mock_execute)

        lazy = _LazyExecute("some.fake.module")

        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            config = MagicMock()
            lazy(config)
            lazy(config)

            # import_module should only be called once
            mock_import.assert_called_once()
            assert mock_execute.call_count == 2


# ---------------------------------------------------------------------------
# get_shared_registry / reset_shared_registry (singleton pattern)
# ---------------------------------------------------------------------------


class TestSharedRegistry:
    def setup_method(self):
        """Ensure each test starts with a clean singleton."""
        reset_shared_registry()

    def teardown_method(self):
        """Clean up after each test."""
        reset_shared_registry()

    def test_get_shared_registry_returns_registry(self):
        """get_shared_registry() returns a SkillRegistry instance."""
        reg = get_shared_registry()
        assert isinstance(reg, SkillRegistry)

    def test_get_shared_registry_is_singleton(self):
        """Calling get_shared_registry() twice returns the same object."""
        reg1 = get_shared_registry()
        reg2 = get_shared_registry()
        assert reg1 is reg2

    def test_reset_clears_singleton(self):
        """After reset_shared_registry(), a new registry is created."""
        reg1 = get_shared_registry()
        reset_shared_registry()
        reg2 = get_shared_registry()
        assert reg1 is not reg2

    def test_get_shared_registry_calls_discover(self):
        """get_shared_registry() automatically calls discover()."""
        with patch.object(SkillRegistry, "discover") as mock_discover:
            reset_shared_registry()
            get_shared_registry()
            mock_discover.assert_called_once()


# ---------------------------------------------------------------------------
# discover() tests
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_entry_point_scanning(self):
        """discover() loads skills from entry points."""
        fake_meta = _make_meta("ep_skill", ["ep_tool"])
        fake_register = MagicMock(return_value=(fake_meta, _dummy_execute))

        fake_ep = MagicMock()
        fake_ep.name = "ep_skill"
        fake_ep.load.return_value = fake_register

        mock_eps = MagicMock()
        mock_eps.select.return_value = [fake_ep]

        reg = SkillRegistry()
        with (
            patch("importlib.metadata.entry_points", return_value=mock_eps),
            patch.object(reg, "_discover_builtins"),
        ):
            reg.discover()

        assert reg.get_skill("ep_skill") is not None
        assert reg.skill_for_tool("ep_tool") == "ep_skill"

    def test_entry_point_load_failure_swallowed(self):
        """If an entry point fails to load, it is swallowed gracefully."""
        broken_ep = MagicMock()
        broken_ep.name = "broken_skill"
        broken_ep.load.side_effect = ImportError("no such module")

        mock_eps = MagicMock()
        mock_eps.select.return_value = [broken_ep]

        reg = SkillRegistry()
        with (
            patch("importlib.metadata.entry_points", return_value=mock_eps),
            patch.object(reg, "_discover_builtins"),
        ):
            # Should not raise
            reg.discover()

        assert reg.get_skill("broken_skill") is None

    def test_discover_builtins_called_after_entry_points(self):
        """_discover_builtins() is called as phase 2 of discovery."""
        mock_eps = MagicMock()
        mock_eps.select.return_value = []

        reg = SkillRegistry()
        with (
            patch("importlib.metadata.entry_points", return_value=mock_eps),
            patch.object(reg, "_discover_builtins") as mock_builtins,
        ):
            reg.discover()
            mock_builtins.assert_called_once()

    def test_no_plugins_warning(self, caplog):
        """When discover finds no skills, a warning is logged."""
        mock_eps = MagicMock()
        mock_eps.select.return_value = []

        reg = SkillRegistry()
        with (
            patch("importlib.metadata.entry_points", return_value=mock_eps),
            patch.object(reg, "_discover_builtins"),
            caplog.at_level("WARNING", logger="creel.skills.registry"),
        ):
            reg.discover()

        assert "no plugins" in caplog.text.lower()

    def test_entry_point_register_failure_swallowed(self):
        """If register_fn() returns bad data, the skill is skipped gracefully."""
        broken_register = MagicMock(side_effect=TypeError("bad return"))

        broken_ep = MagicMock()
        broken_ep.name = "bad_skill"
        broken_ep.load.return_value = broken_register

        mock_eps = MagicMock()
        mock_eps.select.return_value = [broken_ep]

        reg = SkillRegistry()
        with (
            patch("importlib.metadata.entry_points", return_value=mock_eps),
            patch.object(reg, "_discover_builtins"),
        ):
            reg.discover()

        assert reg.get_skill("bad_skill") is None

    def test_discover_builtins_skips_already_registered(self):
        """Built-in discovery does not overwrite skills registered via entry points."""
        # Pre-register a skill whose ID matches a built-in executor
        ep_meta = _make_meta("weather", ["check_weather"])
        ep_execute = MagicMock(return_value="{}")

        reg = SkillRegistry()
        reg.register(ep_meta, ep_execute)

        # Now run _discover_builtins; it should skip weather because it's already present
        reg._discover_builtins()

        # The execute function should still be our mock, not a _LazyExecute
        entry = reg.get_skill("weather")
        assert entry is not None
        assert entry.execute is ep_execute

    def test_entry_points_fallback_without_select(self):
        """Covers the fallback path for Python < 3.12 entry_points() without .select()."""
        fake_meta = _make_meta("old_ep_skill", ["old_ep_tool"])
        fake_register = MagicMock(return_value=(fake_meta, _dummy_execute))

        fake_ep = MagicMock()
        fake_ep.name = "old_ep_skill"
        fake_ep.load.return_value = fake_register

        # Simulate entry_points() that returns a dict (no .select method)
        mock_eps = {"creel.skills": [fake_ep]}

        reg = SkillRegistry()
        with (
            patch("importlib.metadata.entry_points", return_value=mock_eps),
            patch.object(reg, "_discover_builtins"),
        ):
            reg.discover()

        assert reg.get_skill("old_ep_skill") is not None


# ---------------------------------------------------------------------------
# Builtin register_skill() validation for ALL 29 executors
# ---------------------------------------------------------------------------


class TestBuiltinRegisterSkill:
    """Validate that executor register_skill() functions return valid metadata."""

    @pytest.mark.parametrize(
        "module_path,expected_id,expected_tools",
        [
            (
                "executors.weather.executor",
                "weather",
                ["check_weather"],
            ),
            (
                "executors.gcal.executor",
                "gcal",
                ["check_calendar"],
            ),
            (
                "executors.gcal_write.executor",
                "gcal_write",
                ["create_event"],
            ),
            (
                "executors.gmail_readonly.executor",
                "gmail_readonly",
                ["check_email", "read_email"],
            ),
            (
                "executors.gmail_send.executor",
                "gmail_send",
                ["send_email"],
            ),
            (
                "executors.gmail_modify.executor",
                "gmail_modify",
                ["trash_email", "mark_read"],
            ),
            (
                "executors.drive.executor",
                "drive",
                ["check_drive"],
            ),
            (
                "executors.drive_write.executor",
                "drive_write",
                ["upload_file"],
            ),
            (
                "executors.google_docs.executor",
                "google_docs",
                ["read_doc", "create_doc", "append_to_doc", "replace_in_doc"],
            ),
            (
                "executors.google_sheets.executor",
                "google_sheets",
                ["read_sheet", "create_sheet", "write_to_sheet", "append_to_sheet"],
            ),
            (
                "executors.google_slides.executor",
                "google_slides",
                ["read_slides", "create_slides", "add_slide", "replace_in_slides"],
            ),
            (
                "executors.apple_notes.executor",
                "apple_notes",
                ["list_notes", "search_notes", "read_note", "create_note"],
            ),
            (
                "executors.apple_reminders.executor",
                "apple_reminders",
                ["list_reminders", "create_reminder", "complete_reminder", "get_reminder_lists"],
            ),
            (
                "executors.brave_search.executor",
                "brave_search",
                ["web_search"],
            ),
            (
                "executors.notion.executor",
                "notion",
                ["notion_api"],
            ),
            (
                "executors.notion_write.executor",
                "notion_write",
                ["notion_write"],
            ),
            (
                "executors.fetch_url.executor",
                "fetch_url",
                ["fetch_url"],
            ),
            (
                "executors.browser.executor",
                "browser",
                [
                    "browser_open",
                    "browser_navigate",
                    "browser_get_content",
                    "browser_click",
                    "browser_type",
                    "browser_screenshot",
                    "browser_links",
                    "browser_close",
                ],
            ),
            (
                "executors.exec.executor",
                "exec",
                ["exec"],
            ),
            (
                "executors.exec_interactive.executor",
                "exec_interactive",
                [
                    "start_session",
                    "send_input",
                    "read_output",
                    "resize_terminal",
                    "close_session",
                    "session_info",
                    "list_sessions",
                    "get_io_log",
                ],
            ),
            (
                "executors.file_ops.executor",
                "file_ops",
                ["read_file", "write_file", "edit_file", "list_files"],
            ),
            (
                "executors.github.executor",
                "github",
                ["github"],
            ),
            (
                "executors.coding.executor",
                "coding",
                ["coding"],
            ),
            (
                "executors.tts.executor",
                "tts",
                ["synthesize_speech"],
            ),
            (
                "executors.bluebubbles.executor",
                "bluebubbles",
                ["check_messages", "send_imessage", "react_imessage", "get_chats"],
            ),
            (
                "executors.things.executor",
                "things",
                ["list_things", "create_things_task", "complete_things_task"],
            ),
            (
                "executors.git_ops.executor",
                "git_ops",
                ["git_status", "git_diff", "git_log", "git_commit", "git_branch", "git_push"],
            ),
            (
                "executors.imessage_bridge.executor",
                "imessage_bridge",
                ["imessage_send"],
            ),
            (
                "executors.host_exec.executor",
                "host_exec",
                ["host_exec", "host_process", "host_sessions"],
            ),
            (
                "executors.pdf_reader.executor",
                "pdf_reader",
                ["read_pdf", "search_pdf"],
            ),
        ],
    )
    def test_register_skill_returns_valid_meta(self, module_path, expected_id, expected_tools):
        mod = importlib.import_module(module_path)
        register_fn = mod.register_skill
        meta, execute = register_fn()

        assert isinstance(meta, SkillMeta)
        assert meta.id == expected_id
        assert callable(execute)
        assert [t.name for t in meta.tools] == expected_tools

    @pytest.mark.parametrize(
        "module_path",
        [
            "executors.weather.executor",
            "executors.gcal.executor",
            "executors.gcal_write.executor",
            "executors.gmail_readonly.executor",
            "executors.gmail_send.executor",
            "executors.gmail_modify.executor",
            "executors.drive.executor",
            "executors.drive_write.executor",
            "executors.google_docs.executor",
            "executors.google_sheets.executor",
            "executors.google_slides.executor",
            "executors.apple_notes.executor",
            "executors.apple_reminders.executor",
            "executors.brave_search.executor",
            "executors.notion.executor",
            "executors.notion_write.executor",
            "executors.fetch_url.executor",
            "executors.browser.executor",
            "executors.exec.executor",
            "executors.exec_interactive.executor",
            "executors.file_ops.executor",
            "executors.github.executor",
            "executors.coding.executor",
            "executors.tts.executor",
            "executors.bluebubbles.executor",
            "executors.things.executor",
            "executors.git_ops.executor",
            "executors.imessage_bridge.executor",
            "executors.host_exec.executor",
            "executors.pdf_reader.executor",
        ],
    )
    def test_register_skill_integrates_with_registry(self, module_path):
        mod = importlib.import_module(module_path)
        meta, execute = mod.register_skill()

        reg = SkillRegistry()
        reg.register(meta, execute)

        assert reg.get_skill(meta.id) is not None
        for tool in meta.tools:
            assert reg.skill_for_tool(tool.name) == meta.id


class TestBuiltinExecuteFunctions:
    """Test that execute functions from register_skill work correctly."""

    def _make_config(self, name: str, args: dict):
        from creel.models import ExecutorConfig

        return ExecutorConfig(name=name, args=args)

    def test_exec_execute_success(self):
        import json
        from unittest.mock import patch

        from executors.exec.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("exec", {"command": "echo hello"})

        with patch("executors.exec.executor.run_command") as mock_run:
            mock_run.return_value = {"success": True, "stdout": "hello\n"}
            result = execute(config)
            parsed = json.loads(result)
            assert parsed["success"] is True
            mock_run.assert_called_once_with("echo hello", None)

    def test_exec_execute_missing_command(self):
        from executors.exec.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("exec", {})

        with pytest.raises(ValueError, match="exec requires a 'command' argument"):
            execute(config)

    def test_coding_execute_success(self):
        import json
        from unittest.mock import patch

        from executors.coding.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("coding", {"command": "python --version"})

        with patch("executors.coding.executor.run_command") as mock_run:
            mock_run.return_value = {"success": True, "stdout": "Python 3.12\n"}
            result = execute(config)
            parsed = json.loads(result)
            assert parsed["success"] is True
            mock_run.assert_called_once_with(
                "python --version", workdir=None, mount=None, timeout=None
            )

    def test_coding_execute_missing_command(self):
        from executors.coding.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("coding", {})

        with pytest.raises(ValueError, match="coding requires a 'command' argument"):
            execute(config)

    def test_github_execute_success(self):
        import json
        from unittest.mock import patch

        from executors.github.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("github", {"command": "issue list"})

        with patch("executors.github.executor.run_gh_command") as mock_run:
            mock_run.return_value = {"success": True, "stdout": ""}
            result = execute(config)
            parsed = json.loads(result)
            assert parsed["success"] is True
            mock_run.assert_called_once_with("issue list", None)

    def test_github_execute_missing_command(self):
        from executors.github.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("github", {})

        with pytest.raises(ValueError, match="github requires a 'command' argument"):
            execute(config)

    def test_google_slides_execute_unknown_action(self):
        from executors.google_slides.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("google_slides", {"action": "bad_action"})

        with pytest.raises(ValueError, match="unknown action 'bad_action'"):
            execute(config)

    def test_exec_interactive_execute_unknown_action(self):
        from executors.exec_interactive.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("exec_interactive", {"action": "bad_action"})

        with pytest.raises(ValueError, match="unknown action 'bad_action'"):
            execute(config)

    def test_exec_interactive_execute_list_sessions(self):
        import json
        from unittest.mock import patch

        from executors.exec_interactive.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("exec_interactive", {"action": "list_sessions"})

        with patch("executors.exec_interactive.executor.list_sessions") as mock_ls:
            mock_ls.return_value = {"success": True, "sessions": []}
            result = execute(config)
            parsed = json.loads(result)
            assert parsed["success"] is True
            mock_ls.assert_called_once()

    def test_notion_execute_success(self):
        import json
        from unittest.mock import patch

        from executors.notion.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("notion", {"action": "search", "query": "test"})

        with patch("executors.notion.executor.run_action") as mock_run:
            mock_run.return_value = {"results": [], "has_more": False}
            result = execute(config)
            parsed = json.loads(result)
            assert parsed["results"] == []

    def test_notion_write_execute_success(self):
        import json
        from unittest.mock import patch

        from executors.notion_write.executor import register_skill

        _, execute = register_skill()
        config = self._make_config(
            "notion_write",
            {"action": "delete_page", "page_id": "abc123"},
        )

        with patch("executors.notion_write.executor.run_action") as mock_run:
            mock_run.return_value = {"ok": True, "action": "delete_page"}
            result = execute(config)
            parsed = json.loads(result)
            assert parsed["ok"] is True

    def test_file_ops_execute_unknown_action(self):
        from executors.file_ops.executor import register_skill

        _, execute = register_skill()
        config = self._make_config("file_ops", {"action": "bad_action"})

        with pytest.raises(ValueError, match="file_ops: unknown action 'bad_action'"):
            execute(config)

    def test_file_ops_execute_list(self, tmp_path):
        import json

        from executors.file_ops.executor import register_skill

        _, execute = register_skill()
        config = self._make_config(
            "file_ops",
            {
                "action": "list",
                "workspace": str(tmp_path),
                "directory": ".",
            },
        )

        result = execute(config)
        parsed = json.loads(result)
        assert "entries" in parsed
