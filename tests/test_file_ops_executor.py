"""Tests for the file_ops executor."""

from __future__ import annotations

import json
import os

import pytest

from creel.models import ExecutorConfig
from executors.file_ops.executor import (
    _safe_path,
    action_edit,
    action_list,
    action_read,
    action_write,
    main,
)


@pytest.fixture()
def workspace(tmp_path):
    """Create a temporary workspace and set the WORKSPACE env var."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    old = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(ws)
    yield ws
    if old is None:
        os.environ.pop("WORKSPACE", None)
    else:
        os.environ["WORKSPACE"] = old


def _set_env(env: dict[str, str]):
    """Set env vars and return a cleanup function."""
    old_vals = {}
    for k, v in env.items():
        old_vals[k] = os.environ.get(k)
        os.environ[k] = v

    def cleanup():
        for k, old in old_vals.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    return cleanup


class TestSafePath:
    """Tests for path sandboxing."""

    def test_relative_path_within_workspace(self, workspace):
        result = _safe_path("hello.txt")
        assert result == str(workspace / "hello.txt")

    def test_dotdot_traversal_blocked(self, workspace):
        with pytest.raises(ValueError, match="Path escapes workspace"):
            _safe_path("../../../etc/passwd")

    def test_absolute_path_outside_blocked(self, workspace):
        with pytest.raises(ValueError, match="Path escapes workspace"):
            _safe_path("/etc/passwd")

    def test_symlink_traversal_blocked(self, workspace):
        # Create a symlink pointing outside workspace
        link = workspace / "escape"
        link.symlink_to("/etc")
        with pytest.raises(ValueError, match="Path escapes workspace"):
            _safe_path("escape/passwd")

    def test_nested_dotdot_blocked(self, workspace):
        (workspace / "sub").mkdir()
        with pytest.raises(ValueError, match="Path escapes workspace"):
            _safe_path("sub/../../etc/passwd")

    def test_workspace_root_itself(self, workspace):
        result = _safe_path(".")
        assert result == str(workspace)

    def test_workspace_prefix_stripped(self, workspace):
        """Paths starting with 'workspace/' should not double-prefix."""
        result = _safe_path("workspace/scripts/hello.py")
        assert result == str(workspace / "scripts" / "hello.py")

    def test_workspace_prefix_bare(self, workspace):
        """Bare 'workspace' path resolves to workspace root."""
        result = _safe_path("workspace")
        assert result == str(workspace)

    def test_workspace_prefix_with_leading_slash(self, workspace):
        """'/workspace/file.txt' should also be stripped."""
        result = _safe_path("/workspace/file.txt")
        assert result == str(workspace / "file.txt")


class TestActionRead:
    """Tests for the read action."""

    def test_read_file(self, workspace):
        (workspace / "test.txt").write_text("hello\nworld\n")
        cleanup = _set_env({"ACTION": "read", "FILE_PATH": "test.txt"})
        try:
            result = action_read()
            assert result["content"] == "hello\nworld\n"
            assert result["lines"] == 2
            assert result["path"] == "test.txt"
        finally:
            cleanup()

    def test_read_with_offset_and_limit(self, workspace):
        (workspace / "lines.txt").write_text("a\nb\nc\nd\ne\n")
        cleanup = _set_env(
            {
                "ACTION": "read",
                "FILE_PATH": "lines.txt",
                "OFFSET": "1",
                "LIMIT": "2",
            }
        )
        try:
            result = action_read()
            assert result["content"] == "b\nc\n"
            assert result["lines"] == 2
        finally:
            cleanup()

    def test_read_missing_file(self, workspace):
        cleanup = _set_env({"FILE_PATH": "nope.txt"})
        try:
            result = action_read()
            assert "error" in result
            assert "not found" in result["error"].lower()
        finally:
            cleanup()

    def test_read_missing_path_param(self, workspace):
        cleanup = _set_env({"FILE_PATH": ""})
        try:
            result = action_read()
            assert "error" in result
            assert "FILE_PATH is required" in result["error"]
        finally:
            cleanup()

    def test_read_directory_not_file(self, workspace):
        (workspace / "adir").mkdir()
        cleanup = _set_env({"FILE_PATH": "adir"})
        try:
            result = action_read()
            assert "error" in result
            assert "Not a file" in result["error"]
        finally:
            cleanup()

    def test_read_path_traversal(self, workspace):
        cleanup = _set_env({"FILE_PATH": "../../../etc/passwd"})
        try:
            result = action_read()
            assert "error" in result
            assert "escapes workspace" in result["error"]
        finally:
            cleanup()

    def test_read_invalid_offset(self, workspace):
        (workspace / "test.txt").write_text("hello\n")
        cleanup = _set_env({"FILE_PATH": "test.txt", "OFFSET": "abc"})
        try:
            result = action_read()
            assert "error" in result
            assert "must be integers" in result["error"]
        finally:
            cleanup()

    def test_read_invalid_limit(self, workspace):
        (workspace / "test.txt").write_text("hello\n")
        cleanup = _set_env({"FILE_PATH": "test.txt", "LIMIT": "xyz"})
        try:
            result = action_read()
            assert "error" in result
            assert "must be integers" in result["error"]
        finally:
            cleanup()


class TestActionWrite:
    """Tests for the write action."""

    def test_write_new_file(self, workspace):
        cleanup = _set_env({"FILE_PATH": "out.txt", "CONTENT": "hello"})
        try:
            result = action_write()
            assert result["bytes_written"] == 5
            assert (workspace / "out.txt").read_text() == "hello"
        finally:
            cleanup()

    def test_write_creates_parent_dirs(self, workspace):
        cleanup = _set_env({"FILE_PATH": "a/b/c.txt", "CONTENT": "nested"})
        try:
            result = action_write()
            assert "error" not in result
            assert (workspace / "a" / "b" / "c.txt").read_text() == "nested"
        finally:
            cleanup()

    def test_write_path_traversal(self, workspace):
        cleanup = _set_env({"FILE_PATH": "../../pwned.txt", "CONTENT": "bad"})
        try:
            result = action_write()
            assert "error" in result
            assert "escapes workspace" in result["error"]
        finally:
            cleanup()

    def test_write_workspace_prefixed_path(self, workspace):
        """Writing to 'workspace/scripts/hello.py' should not double-prefix."""
        cleanup = _set_env({"FILE_PATH": "workspace/scripts/hello.py", "CONTENT": "print('hi')"})
        try:
            result = action_write()
            assert "error" not in result
            assert (workspace / "scripts" / "hello.py").read_text() == "print('hi')"
            # Ensure no double-prefixed path was created
            assert not (workspace / "workspace").exists()
        finally:
            cleanup()

    def test_write_empty_content(self, workspace):
        cleanup = _set_env({"FILE_PATH": "empty.txt", "CONTENT": ""})
        try:
            result = action_write()
            assert result["bytes_written"] == 0
            assert (workspace / "empty.txt").read_text() == ""
        finally:
            cleanup()

    def test_write_missing_path(self, workspace):
        cleanup = _set_env({"FILE_PATH": "", "CONTENT": "x"})
        try:
            result = action_write()
            assert "error" in result
        finally:
            cleanup()


class TestActionEdit:
    """Tests for the edit action."""

    def test_edit_replaces_text(self, workspace):
        (workspace / "doc.txt").write_text("foo bar baz")
        cleanup = _set_env(
            {
                "FILE_PATH": "doc.txt",
                "OLD_TEXT": "bar",
                "NEW_TEXT": "qux",
            }
        )
        try:
            result = action_edit()
            assert result["replacements"] == 1
            assert (workspace / "doc.txt").read_text() == "foo qux baz"
        finally:
            cleanup()

    def test_edit_multiple_replacements(self, workspace):
        (workspace / "doc.txt").write_text("aaa bbb aaa")
        cleanup = _set_env(
            {
                "FILE_PATH": "doc.txt",
                "OLD_TEXT": "aaa",
                "NEW_TEXT": "ccc",
            }
        )
        try:
            result = action_edit()
            assert result["replacements"] == 2
            assert (workspace / "doc.txt").read_text() == "ccc bbb ccc"
        finally:
            cleanup()

    def test_edit_old_text_not_found(self, workspace):
        (workspace / "doc.txt").write_text("hello")
        cleanup = _set_env(
            {
                "FILE_PATH": "doc.txt",
                "OLD_TEXT": "missing",
                "NEW_TEXT": "x",
            }
        )
        try:
            result = action_edit()
            assert "error" in result
            assert "not found" in result["error"].lower()
        finally:
            cleanup()

    def test_edit_missing_file(self, workspace):
        cleanup = _set_env(
            {
                "FILE_PATH": "nope.txt",
                "OLD_TEXT": "a",
                "NEW_TEXT": "b",
            }
        )
        try:
            result = action_edit()
            assert "error" in result
            assert "not found" in result["error"].lower()
        finally:
            cleanup()

    def test_edit_path_traversal(self, workspace):
        cleanup = _set_env(
            {
                "FILE_PATH": "../../etc/passwd",
                "OLD_TEXT": "root",
                "NEW_TEXT": "hacked",
            }
        )
        try:
            result = action_edit()
            assert "error" in result
            assert "escapes workspace" in result["error"]
        finally:
            cleanup()

    def test_edit_missing_old_text_param(self, workspace):
        (workspace / "doc.txt").write_text("hello")
        cleanup = _set_env(
            {
                "FILE_PATH": "doc.txt",
                "OLD_TEXT": "",
                "NEW_TEXT": "x",
            }
        )
        try:
            result = action_edit()
            assert "error" in result
            assert "OLD_TEXT is required" in result["error"]
        finally:
            cleanup()


class TestActionList:
    """Tests for the list action."""

    def test_list_directory(self, workspace):
        (workspace / "a.txt").write_text("a")
        (workspace / "b.txt").write_text("bb")
        (workspace / "sub").mkdir()
        cleanup = _set_env({"DIRECTORY": ".", "PATTERN": "*"})
        try:
            result = action_list()
            names = [e["name"] for e in result["entries"]]
            assert "a.txt" in names
            assert "b.txt" in names
            assert "sub" in names
            assert result["count"] == 3
        finally:
            cleanup()

    def test_list_with_pattern(self, workspace):
        (workspace / "a.txt").write_text("a")
        (workspace / "b.py").write_text("b")
        cleanup = _set_env({"DIRECTORY": ".", "PATTERN": "*.txt"})
        try:
            result = action_list()
            names = [e["name"] for e in result["entries"]]
            assert names == ["a.txt"]
        finally:
            cleanup()

    def test_list_recursive(self, workspace):
        (workspace / "top.txt").write_text("t")
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "deep.txt").write_text("d")
        cleanup = _set_env(
            {
                "DIRECTORY": ".",
                "PATTERN": "*.txt",
                "RECURSIVE": "true",
            }
        )
        try:
            result = action_list()
            names = [e["name"] for e in result["entries"]]
            assert "top.txt" in names
            assert os.path.join("sub", "deep.txt") in names
        finally:
            cleanup()

    def test_list_missing_directory(self, workspace):
        cleanup = _set_env({"DIRECTORY": "nope"})
        try:
            result = action_list()
            assert "error" in result
            assert "not found" in result["error"].lower()
        finally:
            cleanup()

    def test_list_path_traversal(self, workspace):
        cleanup = _set_env({"DIRECTORY": "../../"})
        try:
            result = action_list()
            assert "error" in result
            assert "escapes workspace" in result["error"]
        finally:
            cleanup()

    def test_list_skips_symlinks(self, workspace):
        (workspace / "real.txt").write_text("real")
        (workspace / "link.txt").symlink_to(workspace / "real.txt")
        cleanup = _set_env({"DIRECTORY": "."})
        try:
            result = action_list()
            names = [e["name"] for e in result["entries"]]
            assert "real.txt" in names
            assert "link.txt" not in names
        finally:
            cleanup()

    def test_list_recursive_skips_symlinks(self, workspace):
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "real.txt").write_text("real")
        (sub / "link.txt").symlink_to(sub / "real.txt")
        cleanup = _set_env(
            {
                "DIRECTORY": ".",
                "PATTERN": "*.txt",
                "RECURSIVE": "true",
            }
        )
        try:
            result = action_list()
            names = [e["name"] for e in result["entries"]]
            assert os.path.join("sub", "real.txt") in names
            assert os.path.join("sub", "link.txt") not in names
        finally:
            cleanup()

    def test_list_entry_types(self, workspace):
        (workspace / "file.txt").write_text("data")
        (workspace / "dir").mkdir()
        cleanup = _set_env({"DIRECTORY": "."})
        try:
            result = action_list()
            by_name = {e["name"]: e for e in result["entries"]}
            assert by_name["file.txt"]["type"] == "file"
            assert by_name["file.txt"]["size"] == 4
            assert by_name["dir"]["type"] == "directory"
        finally:
            cleanup()


class TestMain:
    """Tests for the main entrypoint."""

    def test_unknown_action(self, workspace, capsys):
        cleanup = _set_env({"ACTION": "delete"})
        try:
            with pytest.raises(SystemExit, match="1"):
                main()
            captured = capsys.readouterr()
            output = json.loads(captured.err)
            assert "error" in output
            assert "Unknown action" in output["error"]
        finally:
            cleanup()

    def test_main_read(self, workspace, capsys):
        (workspace / "f.txt").write_text("content")
        cleanup = _set_env({"ACTION": "read", "FILE_PATH": "f.txt"})
        try:
            main()
            captured = capsys.readouterr()
            output = json.loads(captured.out)
            assert output["content"] == "content"
        finally:
            cleanup()

    def test_main_error_exits_nonzero(self, workspace, capsys):
        cleanup = _set_env({"ACTION": "read", "FILE_PATH": "missing.txt"})
        try:
            with pytest.raises(SystemExit, match="1"):
                main()
            captured = capsys.readouterr()
            assert captured.out == ""
            output = json.loads(captured.err)
            assert "error" in output
        finally:
            cleanup()


class TestExecFileOpsInline:
    """Tests for the file_ops skill execute function."""

    @pytest.fixture(autouse=True)
    def _register(self):
        """Register the file_ops skill and expose its execute function."""
        from executors.file_ops.executor import register_skill

        _meta, self._execute = register_skill()

    def test_read_action(self, workspace):
        (workspace / "hello.txt").write_text("hello inline")
        config = ExecutorConfig(
            name="file_ops",
            args={"action": "read", "file_path": "hello.txt"},
        )
        result = json.loads(self._execute(config))
        assert result["content"] == "hello inline"

    def test_write_action(self, workspace):
        config = ExecutorConfig(
            name="file_ops",
            args={"action": "write", "file_path": "new.txt", "content": "written"},
        )
        result = json.loads(self._execute(config))
        assert result["bytes_written"] == 7
        assert (workspace / "new.txt").read_text() == "written"

    def test_list_action(self, workspace):
        (workspace / "a.txt").write_text("a")
        (workspace / "b.txt").write_text("b")
        config = ExecutorConfig(
            name="file_ops",
            args={"action": "list", "directory": "."},
        )
        result = json.loads(self._execute(config))
        names = [e["name"] for e in result["entries"]]
        assert "a.txt" in names
        assert "b.txt" in names

    def test_edit_action(self, workspace):
        (workspace / "doc.txt").write_text("foo bar baz")
        config = ExecutorConfig(
            name="file_ops",
            args={
                "action": "edit",
                "file_path": "doc.txt",
                "old_text": "bar",
                "new_text": "qux",
            },
        )
        result = json.loads(self._execute(config))
        assert result["replacements"] == 1
        assert (workspace / "doc.txt").read_text() == "foo qux baz"

    def test_unknown_action_raises(self, workspace):
        config = ExecutorConfig(
            name="file_ops",
            args={"action": "delete"},
        )
        with pytest.raises(ValueError, match="unknown action"):
            self._execute(config)

    def test_workspace_arg_overrides_env(self, tmp_path):
        """The workspace arg should set WORKSPACE env for the executor."""
        ws = tmp_path / "custom_ws"
        ws.mkdir()
        (ws / "f.txt").write_text("custom")

        config = ExecutorConfig(
            name="file_ops",
            args={
                "action": "read",
                "file_path": "f.txt",
                "workspace": str(ws),
            },
        )
        result = json.loads(self._execute(config))
        assert result["content"] == "custom"

    def test_env_restored_after_call(self, workspace):
        """Environment variables should be restored after execution."""
        old_workspace = os.environ.get("WORKSPACE")
        config = ExecutorConfig(
            name="file_ops",
            args={"action": "list", "directory": "."},
        )
        self._execute(config)
        assert os.environ.get("WORKSPACE") == old_workspace
