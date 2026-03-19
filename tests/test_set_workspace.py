"""Tests for the set_workspace built-in tool and workspace injection."""

from __future__ import annotations

import json
import os

import pytest

from creel.models import SkillOverride
from creel.skills.registry import SkillRegistry
from creel.tools import (
    _is_blocked_path,
    _validate_workspace_path,
    build_tool_definitions,
    execute_tool_call,
)

# ---------------------------------------------------------------------------
# _is_blocked_path
# ---------------------------------------------------------------------------


class TestIsBlockedPath:
    def test_root_blocked(self):
        assert _is_blocked_path("/") is True

    def test_system_dirs_blocked(self):
        for d in ["/etc", "/var", "/usr", "/bin", "/sbin", "/dev", "/proc"]:
            assert _is_blocked_path(d) is True, f"Expected {d} to be blocked"

    def test_system_subdirs_blocked(self):
        """Prefix matching: subdirectories of system dirs are also blocked."""
        assert _is_blocked_path("/etc/nginx") is True
        assert _is_blocked_path("/var/log/syslog") is True
        assert _is_blocked_path("/usr/local/bin") is True

    def test_sensitive_home_dirs_blocked(self):
        home = os.path.expanduser("~")
        if home == "~":
            pytest.skip("Cannot determine home directory")
        for name in (".ssh", ".gnupg", ".age", ".aws"):
            assert _is_blocked_path(os.path.join(home, name)) is True
            # Subdirectories too
            assert _is_blocked_path(os.path.join(home, name, "keys")) is True

    def test_normal_dirs_not_blocked(self, tmp_path):
        assert _is_blocked_path(str(tmp_path)) is False

    def test_home_itself_not_blocked(self):
        home = os.path.expanduser("~")
        if home == "~":
            pytest.skip("Cannot determine home directory")
        assert _is_blocked_path(home) is False


# ---------------------------------------------------------------------------
# _validate_workspace_path
# ---------------------------------------------------------------------------


class TestValidateWorkspacePath:
    def test_valid_directory(self, tmp_path):
        assert _validate_workspace_path(str(tmp_path)) is None

    def test_relative_path_rejected(self):
        error = _validate_workspace_path("relative/path")
        assert error is not None
        assert "absolute" in error.lower()

    def test_nonexistent_path_rejected(self):
        error = _validate_workspace_path("/tmp/nonexistent_abc_xyz_12345")
        assert error is not None
        assert "does not exist" in error.lower()

    def test_file_not_dir_rejected(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        error = _validate_workspace_path(str(f))
        assert error is not None
        assert "not a directory" in error.lower()

    def test_dangerous_root_rejected(self):
        for root in ["/", "/etc", "/var", "/usr", "/bin"]:
            error = _validate_workspace_path(root)
            assert error is not None, f"Expected rejection for {root}"

    def test_system_subdir_rejected(self):
        """Subdirectories of system dirs are also blocked."""
        # /etc/pam.d exists on macOS/Linux
        if os.path.isdir("/etc/pam.d"):
            error = _validate_workspace_path("/etc/pam.d")
            assert error is not None

    def test_generic_error_messages(self):
        """Error messages should not leak path details."""
        error = _validate_workspace_path("/")
        assert error is not None
        # Should NOT contain the actual path in the message
        assert "Cannot use this path" in error

    def test_symlink_resolved(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert _validate_workspace_path(str(link)) is None


# ---------------------------------------------------------------------------
# set_workspace handler
# ---------------------------------------------------------------------------


class TestSetWorkspaceHandler:
    def test_set_valid_workspace(self, tmp_path):
        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": str(tmp_path)},
            registry=SkillRegistry(),
            skill_overrides={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert result["status"] == "ok"
        assert state["workspace"] == os.path.realpath(str(tmp_path))

    def test_set_workspace_relative_path_error(self):
        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": "relative/dir"},
            registry=SkillRegistry(),
            skill_overrides={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result
        assert "workspace" not in state

    def test_set_workspace_nonexistent_error(self):
        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": "/tmp/does_not_exist_xyz_999"},
            registry=SkillRegistry(),
            skill_overrides={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result
        assert "workspace" not in state

    def test_set_workspace_file_not_dir_error(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": str(f)},
            registry=SkillRegistry(),
            skill_overrides={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result

    def test_set_workspace_dangerous_root_error(self):
        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": "/"},
            registry=SkillRegistry(),
            skill_overrides={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result

    def test_set_workspace_empty_path_error(self):
        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": ""},
            registry=SkillRegistry(),
            skill_overrides={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result

    def test_set_workspace_no_session_state_rejected(self, tmp_path):
        """set_workspace without session_state should fail (task mode guard)."""
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": str(tmp_path)},
            registry=SkillRegistry(),
            skill_overrides={},
            session_state=None,
        )
        result = json.loads(result_str)
        assert "error" in result
        assert "interactive" in result["error"].lower()

    def test_set_workspace_resolves_symlinks(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": str(link)},
            registry=SkillRegistry(),
            skill_overrides={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert result["status"] == "ok"
        # Should store the resolved real path
        assert state["workspace"] == str(real)


# ---------------------------------------------------------------------------
# Workspace injection for file_ops tools
# ---------------------------------------------------------------------------


def _file_ops_registry_and_overrides():
    """Return a registry with file_ops and appropriate overrides."""
    registry = SkillRegistry()
    registry._discover_builtins()
    overrides = {"file_ops": SkillOverride(enabled=True)}
    return registry, overrides


class TestWorkspaceInjection:
    def test_file_ops_uses_workspace_from_session_state(self, tmp_path):
        """When workspace is set in session_state, file_ops should use it."""
        (tmp_path / "hello.txt").write_text("hello world")

        state = {"workspace": str(tmp_path)}
        registry, overrides = _file_ops_registry_and_overrides()

        result_str = execute_tool_call(
            tool_name="read_file",
            tool_input={"file_path": "hello.txt"},
            registry=registry,
            skill_overrides=overrides,
            session_state=state,
        )
        result = json.loads(result_str)
        assert result["content"] == "hello world"

    def test_file_ops_without_workspace_uses_default(self, tmp_path):
        """Without workspace in session_state, file_ops uses WORKSPACE env var."""
        (tmp_path / "default.txt").write_text("default content")

        old = os.environ.get("WORKSPACE")
        os.environ["WORKSPACE"] = str(tmp_path)
        try:
            registry, overrides = _file_ops_registry_and_overrides()
            result_str = execute_tool_call(
                tool_name="read_file",
                tool_input={"file_path": "default.txt"},
                registry=registry,
                skill_overrides=overrides,
                session_state={},
            )
            result = json.loads(result_str)
            assert result["content"] == "default content"
        finally:
            if old is None:
                os.environ.pop("WORKSPACE", None)
            else:
                os.environ["WORKSPACE"] = old

    def test_workspace_persists_across_calls(self, tmp_path):
        """Session state should persist workspace across multiple tool calls."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")

        state: dict = {}
        registry, overrides = _file_ops_registry_and_overrides()

        # First: set workspace
        execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": str(tmp_path)},
            registry=registry,
            skill_overrides=overrides,
            session_state=state,
        )

        # Second: read a file using the workspace
        result_str = execute_tool_call(
            tool_name="read_file",
            tool_input={"file_path": "a.txt"},
            registry=registry,
            skill_overrides=overrides,
            session_state=state,
        )
        result = json.loads(result_str)
        assert result["content"] == "aaa"

        # Third: read another file
        result_str = execute_tool_call(
            tool_name="read_file",
            tool_input={"file_path": "b.txt"},
            registry=registry,
            skill_overrides=overrides,
            session_state=state,
        )
        result = json.loads(result_str)
        assert result["content"] == "bbb"

    def test_workspace_change_mid_session(self, tmp_path):
        """Changing workspace mid-session should use the new path."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()
        (dir1 / "f.txt").write_text("from dir1")
        (dir2 / "f.txt").write_text("from dir2")

        state: dict = {}
        registry, overrides = _file_ops_registry_and_overrides()

        # Set to dir1
        execute_tool_call(
            "set_workspace", {"path": str(dir1)}, registry, overrides, session_state=state
        )
        r1 = json.loads(
            execute_tool_call(
                "read_file", {"file_path": "f.txt"}, registry, overrides, session_state=state
            )
        )
        assert r1["content"] == "from dir1"

        # Switch to dir2
        execute_tool_call(
            "set_workspace", {"path": str(dir2)}, registry, overrides, session_state=state
        )
        r2 = json.loads(
            execute_tool_call(
                "read_file", {"file_path": "f.txt"}, registry, overrides, session_state=state
            )
        )
        assert r2["content"] == "from dir2"


# ---------------------------------------------------------------------------
# Security: workspace key stripping and re-validation
# ---------------------------------------------------------------------------


class TestWorkspaceSecurity:
    def test_workspace_key_stripped_from_llm_input(self, tmp_path):
        """LLM cannot inject workspace key in tool_input to bypass set_workspace."""
        legit_ws = tmp_path / "legit"
        evil_ws = tmp_path / "evil"
        legit_ws.mkdir()
        evil_ws.mkdir()
        (legit_ws / "f.txt").write_text("legit content")
        (evil_ws / "f.txt").write_text("evil content")

        state = {"workspace": str(legit_ws)}
        registry, overrides = _file_ops_registry_and_overrides()

        # LLM tries to override workspace via tool_input
        result_str = execute_tool_call(
            tool_name="read_file",
            tool_input={"file_path": "f.txt", "workspace": str(evil_ws)},
            registry=registry,
            skill_overrides=overrides,
            session_state=state,
        )
        result = json.loads(result_str)
        # Should read from legit workspace, not evil
        assert result["content"] == "legit content"

    def test_workspace_revalidated_on_use(self, tmp_path):
        """If workspace directory is deleted after set_workspace, file_ops fails."""
        ws = tmp_path / "ws"
        ws.mkdir()

        state = {"workspace": str(ws)}
        registry, overrides = _file_ops_registry_and_overrides()

        # Delete the workspace directory
        ws.rmdir()

        result_str = execute_tool_call(
            tool_name="read_file",
            tool_input={"file_path": "f.txt"},
            registry=registry,
            skill_overrides=overrides,
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result
        assert "no longer valid" in result["error"].lower()


# ---------------------------------------------------------------------------
# build_tool_definitions includes workspace tools
# ---------------------------------------------------------------------------


class TestBuildToolDefinitions:
    def test_workspace_tools_included(self):
        registry = SkillRegistry()
        defs = build_tool_definitions(registry, {}, include_workspace_tools=True)
        names = [d["name"] for d in defs]
        assert "set_workspace" in names

    def test_workspace_tools_excluded_by_default(self):
        registry = SkillRegistry()
        defs = build_tool_definitions(registry, {})
        names = [d["name"] for d in defs]
        assert "set_workspace" not in names

    def test_workspace_and_memory_tools(self):
        registry = SkillRegistry()
        defs = build_tool_definitions(
            registry,
            {},
            include_memory_tools=True,
            include_workspace_tools=True,
        )
        names = [d["name"] for d in defs]
        assert "set_workspace" in names
        assert "remember" in names
