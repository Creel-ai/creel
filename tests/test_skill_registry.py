"""Tests for the SkillRegistry."""

import pytest

from creel.skills.models import SkillMeta, ToolSpec
from creel.skills.registry import SkillRegistry


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


class TestBuiltinRegisterSkill:
    """Validate that executor register_skill() functions return valid metadata."""

    @pytest.mark.parametrize(
        "module_path,expected_id,expected_tools",
        [
            (
                "executors.google_slides.executor",
                "google_slides",
                ["read_slides", "create_slides", "add_slide", "replace_in_slides"],
            ),
            ("executors.notion.executor", "notion", ["notion_api"]),
            ("executors.notion_write.executor", "notion_write", ["notion_write"]),
            ("executors.exec.executor", "exec", ["exec"]),
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
            ("executors.coding.executor", "coding", ["coding"]),
            ("executors.github.executor", "github", ["github"]),
            (
                "executors.file_ops.executor",
                "file_ops",
                ["read_file", "write_file", "edit_file", "list_files"],
            ),
        ],
    )
    def test_register_skill_returns_valid_meta(self, module_path, expected_id, expected_tools):
        import importlib

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
            "executors.google_slides.executor",
            "executors.notion.executor",
            "executors.notion_write.executor",
            "executors.exec.executor",
            "executors.exec_interactive.executor",
            "executors.coding.executor",
            "executors.github.executor",
            "executors.file_ops.executor",
        ],
    )
    def test_register_skill_integrates_with_registry(self, module_path):
        import importlib

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
