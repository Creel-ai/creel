"""Tests for the set_workspace built-in tool and workspace injection."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from taskrunner.tools import (
    BUILTIN_WORKSPACE_TOOLS,
    _validate_workspace_path,
    build_tool_definitions,
    execute_tool_call,
)
from taskrunner.models import ToolConfig, ToolParameter


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
        assert "does not exist" in error

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
            assert "dangerous" in error.lower()

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
            tools_config={},
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
            tools_config={},
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
            tools_config={},
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
            tools_config={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result

    def test_set_workspace_dangerous_root_error(self):
        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": "/"},
            tools_config={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result
        assert "dangerous" in result["error"].lower()

    def test_set_workspace_empty_path_error(self):
        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": ""},
            tools_config={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert "error" in result

    def test_set_workspace_no_session_state(self, tmp_path):
        """set_workspace without session_state should still succeed."""
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": str(tmp_path)},
            tools_config={},
            session_state=None,
        )
        result = json.loads(result_str)
        assert result["status"] == "ok"

    def test_set_workspace_resolves_symlinks(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        state: dict = {}
        result_str = execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": str(link)},
            tools_config={},
            session_state=state,
        )
        result = json.loads(result_str)
        assert result["status"] == "ok"
        # Should store the resolved real path
        assert state["workspace"] == str(real)


# ---------------------------------------------------------------------------
# Workspace injection for file_ops tools
# ---------------------------------------------------------------------------

def _make_file_ops_tools() -> dict[str, ToolConfig]:
    return {
        "read_file": ToolConfig(
            executor="file_ops",
            description="Read a file",
            parameters={
                "file_path": ToolParameter(
                    type="string",
                    description="Path relative to workspace",
                    required=True,
                ),
            },
            fixed_args={"action": "read"},
        ),
    }


class TestWorkspaceInjection:

    def test_file_ops_uses_workspace_from_session_state(self, tmp_path):
        """When workspace is set in session_state, file_ops should use it."""
        # Create a file in the workspace
        (tmp_path / "hello.txt").write_text("hello world")

        state = {"workspace": str(tmp_path)}
        tools = _make_file_ops_tools()

        result_str = execute_tool_call(
            tool_name="read_file",
            tool_input={"file_path": "hello.txt"},
            tools_config=tools,
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
            tools = _make_file_ops_tools()
            result_str = execute_tool_call(
                tool_name="read_file",
                tool_input={"file_path": "default.txt"},
                tools_config=tools,
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
        tools = _make_file_ops_tools()

        # First: set workspace
        execute_tool_call(
            tool_name="set_workspace",
            tool_input={"path": str(tmp_path)},
            tools_config=tools,
            session_state=state,
        )

        # Second: read a file using the workspace
        result_str = execute_tool_call(
            tool_name="read_file",
            tool_input={"file_path": "a.txt"},
            tools_config=tools,
            session_state=state,
        )
        result = json.loads(result_str)
        assert result["content"] == "aaa"

        # Third: read another file
        result_str = execute_tool_call(
            tool_name="read_file",
            tool_input={"file_path": "b.txt"},
            tools_config=tools,
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
        tools = _make_file_ops_tools()

        # Set to dir1
        execute_tool_call("set_workspace", {"path": str(dir1)}, tools, session_state=state)
        r1 = json.loads(execute_tool_call("read_file", {"file_path": "f.txt"}, tools, session_state=state))
        assert r1["content"] == "from dir1"

        # Switch to dir2
        execute_tool_call("set_workspace", {"path": str(dir2)}, tools, session_state=state)
        r2 = json.loads(execute_tool_call("read_file", {"file_path": "f.txt"}, tools, session_state=state))
        assert r2["content"] == "from dir2"


# ---------------------------------------------------------------------------
# build_tool_definitions includes workspace tools
# ---------------------------------------------------------------------------

class TestBuildToolDefinitions:

    def test_workspace_tools_included(self):
        defs = build_tool_definitions({}, include_workspace_tools=True)
        names = [d["name"] for d in defs]
        assert "set_workspace" in names

    def test_workspace_tools_excluded_by_default(self):
        defs = build_tool_definitions({})
        names = [d["name"] for d in defs]
        assert "set_workspace" not in names

    def test_workspace_and_memory_tools(self):
        defs = build_tool_definitions(
            {}, include_memory_tools=True, include_workspace_tools=True,
        )
        names = [d["name"] for d in defs]
        assert "set_workspace" in names
        assert "remember" in names
