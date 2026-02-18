"""Tests for the git_ops executor."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from executors.git_ops.executor import call_bridge, status, diff, log, commit, branch, push


class TestBridgeClient:
    """Test the bridge client functionality in the git_ops executor."""

    @patch("executors.git_ops.executor.requests.post")
    def test_call_bridge_success(self, mock_post):
        """Test successful bridge call."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "success"}
        mock_post.return_value = mock_response

        with patch.dict(os.environ, {
            "BRIDGE_URL": "http://localhost:8099",
            "BRIDGE_TOKEN": "test-token"
        }):
            result = call_bridge("/git/status")

        assert result["ok"] is True
        assert result["output"] == "success"

        mock_post.assert_called_once_with(
            "http://localhost:8099/git/status",
            json={},
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            timeout=30
        )

    @patch("executors.git_ops.executor.requests.post")
    def test_call_bridge_with_data(self, mock_post):
        """Test bridge call with request data."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True, "output": "diff output"}
        mock_post.return_value = mock_response

        with patch.dict(os.environ, {
            "BRIDGE_URL": "http://localhost:8099",
            "BRIDGE_TOKEN": "test-token"
        }):
            result = call_bridge("/git/diff", {"cached": True})

        assert result["ok"] is True
        mock_post.assert_called_once_with(
            "http://localhost:8099/git/diff",
            json={"cached": True},
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
            timeout=30
        )

    def test_call_bridge_missing_url(self):
        """Test that missing BRIDGE_URL raises error."""
        with patch.dict(os.environ, {"BRIDGE_TOKEN": "test-token"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_URL environment variable not set"):
                call_bridge("/git/status")

    def test_call_bridge_missing_token(self):
        """Test that missing BRIDGE_TOKEN raises error."""
        with patch.dict(os.environ, {"BRIDGE_URL": "http://localhost:8099"}, clear=True):
            with pytest.raises(RuntimeError, match="BRIDGE_TOKEN environment variable not set"):
                call_bridge("/git/status")

    @patch("executors.git_ops.executor.requests.post")
    def test_call_bridge_api_error(self, mock_post):
        """Test handling of bridge API errors."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": False, "error": "Command failed"}
        mock_post.return_value = mock_response

        with patch.dict(os.environ, {
            "BRIDGE_URL": "http://localhost:8099",
            "BRIDGE_TOKEN": "test-token"
        }):
            with pytest.raises(RuntimeError, match="Bridge error: Command failed"):
                call_bridge("/git/status")

    @patch("executors.git_ops.executor.requests.post")
    def test_call_bridge_request_exception(self, mock_post):
        """Test handling of request exceptions."""
        import requests as req
        mock_post.side_effect = req.exceptions.ConnectionError("Connection refused")

        with patch.dict(os.environ, {
            "BRIDGE_URL": "http://localhost:8099",
            "BRIDGE_TOKEN": "test-token"
        }):
            with pytest.raises(RuntimeError, match="Bridge request failed"):
                call_bridge("/git/status")


class TestGitOperations:
    """Test git operations that call the bridge."""

    @patch("executors.git_ops.executor.call_bridge")
    def test_status_default(self, mock_call_bridge):
        """Test status with default parameters."""
        mock_call_bridge.return_value = {"ok": True, "output": "On branch main"}

        result = status()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/status", {})

    @patch("executors.git_ops.executor.call_bridge")
    def test_status_short(self, mock_call_bridge):
        """Test status with short flag."""
        mock_call_bridge.return_value = {"ok": True, "output": "M file.py"}

        result = status(short=True)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/status", {"short": True})

    @patch("executors.git_ops.executor.call_bridge")
    def test_diff_default(self, mock_call_bridge):
        """Test diff with default parameters."""
        mock_call_bridge.return_value = {"ok": True, "output": ""}

        result = diff()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/diff", {})

    @patch("executors.git_ops.executor.call_bridge")
    def test_diff_cached(self, mock_call_bridge):
        """Test diff with cached flag."""
        mock_call_bridge.return_value = {"ok": True, "output": "staged changes"}

        result = diff(cached=True)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/diff", {"cached": True})

    @patch("executors.git_ops.executor.call_bridge")
    def test_diff_with_path(self, mock_call_bridge):
        """Test diff with path filter."""
        mock_call_bridge.return_value = {"ok": True, "output": "file diff"}

        result = diff(path="src/main.py")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/diff", {"path": "src/main.py"})

    @patch("executors.git_ops.executor.call_bridge")
    def test_log_default(self, mock_call_bridge):
        """Test log with default parameters."""
        mock_call_bridge.return_value = {"ok": True, "output": "abc1234 Initial commit"}

        result = log()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/log", {"max_count": 10, "oneline": True})

    @patch("executors.git_ops.executor.call_bridge")
    def test_log_custom(self, mock_call_bridge):
        """Test log with custom parameters."""
        mock_call_bridge.return_value = {"ok": True, "output": "commits"}

        result = log(max_count=5, oneline=False)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/log", {"max_count": 5, "oneline": False})

    @patch("executors.git_ops.executor.call_bridge")
    def test_commit_basic(self, mock_call_bridge):
        """Test commit with message only."""
        mock_call_bridge.return_value = {"ok": True, "output": "1 file changed"}

        result = commit("Fix bug")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/commit", {"message": "Fix bug"})

    @patch("executors.git_ops.executor.call_bridge")
    def test_commit_all(self, mock_call_bridge):
        """Test commit with -a flag."""
        mock_call_bridge.return_value = {"ok": True, "output": "committed"}

        result = commit("Update all", all=True)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/commit", {"message": "Update all", "all": True})

    @patch("executors.git_ops.executor.call_bridge")
    def test_branch_list(self, mock_call_bridge):
        """Test branch listing."""
        mock_call_bridge.return_value = {"ok": True, "output": "* main\n  feature"}

        result = branch()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/branch", {})

    @patch("executors.git_ops.executor.call_bridge")
    def test_branch_list_all(self, mock_call_bridge):
        """Test branch listing with remotes."""
        mock_call_bridge.return_value = {"ok": True, "output": "branches"}

        result = branch(list_all=True)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/branch", {"list_all": True})

    @patch("executors.git_ops.executor.call_bridge")
    def test_branch_create(self, mock_call_bridge):
        """Test creating a branch."""
        mock_call_bridge.return_value = {"ok": True, "output": ""}

        result = branch(name="feature/new")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/branch", {"name": "feature/new"})

    @patch("executors.git_ops.executor.call_bridge")
    def test_branch_delete(self, mock_call_bridge):
        """Test deleting a branch."""
        mock_call_bridge.return_value = {"ok": True, "output": "Deleted branch"}

        result = branch(name="old-branch", delete=True)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/branch", {"name": "old-branch", "delete": True})

    @patch("executors.git_ops.executor.call_bridge")
    def test_push_default(self, mock_call_bridge):
        """Test push with defaults."""
        mock_call_bridge.return_value = {"ok": True, "output": "pushed"}

        result = push()

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/push", {"remote": "origin"}, timeout=60)

    @patch("executors.git_ops.executor.call_bridge")
    def test_push_with_branch(self, mock_call_bridge):
        """Test push with specific branch."""
        mock_call_bridge.return_value = {"ok": True, "output": "pushed"}

        result = push(branch_name="main")

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with("/git/push", {"remote": "origin", "branch": "main"}, timeout=60)

    @patch("executors.git_ops.executor.call_bridge")
    def test_push_set_upstream(self, mock_call_bridge):
        """Test push with set-upstream flag."""
        mock_call_bridge.return_value = {"ok": True, "output": "pushed"}

        result = push(remote="upstream", branch_name="feature", set_upstream=True)

        assert result["ok"] is True
        mock_call_bridge.assert_called_once_with(
            "/git/push",
            {"remote": "upstream", "branch": "feature", "set_upstream": True},
            timeout=60
        )


class TestMainFunction:
    """Test the main executor function."""

    @patch("executors.git_ops.executor.status")
    @patch("builtins.print")
    def test_main_status_default(self, mock_print, mock_status):
        """Test main with default status action."""
        mock_status.return_value = {"ok": True, "output": "On branch main"}

        with patch.dict(os.environ, {}, clear=True):
            from executors.git_ops.executor import main
            main()

        mock_status.assert_called_once_with(False)
        mock_print.assert_called_once_with("On branch main")

    @patch("executors.git_ops.executor.status")
    @patch("builtins.print")
    def test_main_status_short(self, mock_print, mock_status):
        """Test main with short status."""
        mock_status.return_value = {"ok": True, "output": "M file.py"}

        with patch.dict(os.environ, {"ACTION": "status", "SHORT": "true"}):
            from executors.git_ops.executor import main
            main()

        mock_status.assert_called_once_with(True)
        mock_print.assert_called_once_with("M file.py")

    @patch("executors.git_ops.executor.diff")
    @patch("builtins.print")
    def test_main_diff(self, mock_print, mock_diff):
        """Test main with diff action."""
        mock_diff.return_value = {"ok": True, "output": "diff output"}

        with patch.dict(os.environ, {"ACTION": "diff"}):
            from executors.git_ops.executor import main
            main()

        mock_diff.assert_called_once_with(False, None)
        mock_print.assert_called_once_with("diff output")

    @patch("executors.git_ops.executor.diff")
    @patch("builtins.print")
    def test_main_diff_cached_with_path(self, mock_print, mock_diff):
        """Test main with diff action, cached and path."""
        mock_diff.return_value = {"ok": True, "output": "cached diff"}

        with patch.dict(os.environ, {"ACTION": "diff", "CACHED": "true", "PATH_FILTER": "src/main.py"}):
            from executors.git_ops.executor import main
            main()

        mock_diff.assert_called_once_with(True, "src/main.py")
        mock_print.assert_called_once_with("cached diff")

    @patch("executors.git_ops.executor.log")
    @patch("builtins.print")
    def test_main_log(self, mock_print, mock_log):
        """Test main with log action."""
        mock_log.return_value = {"ok": True, "output": "abc1234 commit"}

        with patch.dict(os.environ, {"ACTION": "log"}):
            from executors.git_ops.executor import main
            main()

        mock_log.assert_called_once_with(10, True)
        mock_print.assert_called_once_with("abc1234 commit")

    @patch("executors.git_ops.executor.log")
    @patch("builtins.print")
    def test_main_log_custom(self, mock_print, mock_log):
        """Test main with log action and custom params."""
        mock_log.return_value = {"ok": True, "output": "commits"}

        with patch.dict(os.environ, {"ACTION": "log", "MAX_COUNT": "5", "ONELINE": "false"}):
            from executors.git_ops.executor import main
            main()

        mock_log.assert_called_once_with(5, False)
        mock_print.assert_called_once_with("commits")

    @patch("executors.git_ops.executor.commit")
    @patch("builtins.print")
    def test_main_commit(self, mock_print, mock_commit):
        """Test main with commit action."""
        mock_commit.return_value = {"ok": True, "output": "1 file changed"}

        with patch.dict(os.environ, {"ACTION": "commit", "MESSAGE": "Fix bug"}):
            from executors.git_ops.executor import main
            main()

        mock_commit.assert_called_once_with("Fix bug", False)
        mock_print.assert_called_once_with("1 file changed")

    @patch("executors.git_ops.executor.commit")
    @patch("builtins.print")
    def test_main_commit_all(self, mock_print, mock_commit):
        """Test main with commit -a action."""
        mock_commit.return_value = {"ok": True, "output": "committed"}

        with patch.dict(os.environ, {"ACTION": "commit", "MESSAGE": "Update", "ALL": "true"}):
            from executors.git_ops.executor import main
            main()

        mock_commit.assert_called_once_with("Update", True)

    @patch("builtins.print")
    def test_main_commit_missing_message(self, mock_print):
        """Test main with commit action but missing message."""
        with patch.dict(os.environ, {"ACTION": "commit"}, clear=True):
            with pytest.raises(SystemExit) as excinfo:
                from executors.git_ops.executor import main
                main()

        assert excinfo.value.code == 1

    @patch("executors.git_ops.executor.branch")
    @patch("builtins.print")
    def test_main_branch_list(self, mock_print, mock_branch):
        """Test main with branch list action."""
        mock_branch.return_value = {"ok": True, "output": "* main"}

        with patch.dict(os.environ, {"ACTION": "branch"}):
            from executors.git_ops.executor import main
            main()

        mock_branch.assert_called_once_with(None, False, False)
        mock_print.assert_called_once_with("* main")

    @patch("executors.git_ops.executor.branch")
    @patch("builtins.print")
    def test_main_branch_create(self, mock_print, mock_branch):
        """Test main with branch create action."""
        mock_branch.return_value = {"ok": True, "output": ""}

        with patch.dict(os.environ, {"ACTION": "branch", "BRANCH_NAME": "feature/new"}):
            from executors.git_ops.executor import main
            main()

        mock_branch.assert_called_once_with("feature/new", False, False)

    @patch("executors.git_ops.executor.push")
    @patch("builtins.print")
    def test_main_push(self, mock_print, mock_push):
        """Test main with push action."""
        mock_push.return_value = {"ok": True, "output": "pushed"}

        with patch.dict(os.environ, {"ACTION": "push"}):
            from executors.git_ops.executor import main
            main()

        mock_push.assert_called_once_with("origin", None, False)
        mock_print.assert_called_once_with("pushed")

    @patch("executors.git_ops.executor.push")
    @patch("builtins.print")
    def test_main_push_with_options(self, mock_print, mock_push):
        """Test main with push action and options."""
        mock_push.return_value = {"ok": True, "output": "pushed"}

        with patch.dict(os.environ, {
            "ACTION": "push",
            "REMOTE": "upstream",
            "BRANCH_NAME": "main",
            "SET_UPSTREAM": "true"
        }):
            from executors.git_ops.executor import main
            main()

        mock_push.assert_called_once_with("upstream", "main", True)

    @patch("builtins.print")
    def test_main_unknown_action(self, mock_print):
        """Test main with unknown action."""
        with patch.dict(os.environ, {"ACTION": "unknown"}):
            with pytest.raises(SystemExit) as excinfo:
                from executors.git_ops.executor import main
                main()

        assert excinfo.value.code == 1


class TestBridgeEndpoints:
    """Test the bridge server git endpoints."""

    @pytest.fixture
    def client(self):
        """Create a test client for the bridge server."""
        from fastapi.testclient import TestClient

        # Set up scoped tokens before creating client
        from bridge.server import SCOPED_TOKENS
        SCOPED_TOKENS["GIT"] = "test-git-token"

        from bridge.server import app
        return TestClient(app)

    @pytest.fixture
    def auth_headers(self):
        """Auth headers for git endpoints."""
        return {"Authorization": "Bearer test-git-token"}

    def test_git_status_endpoint(self, client, auth_headers):
        """Test /git/status endpoint."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="On branch main", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "On branch main", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/status", json={}, headers=auth_headers)

        assert response.status_code == 200
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["git", "status"]

    def test_git_status_short_endpoint(self, client, auth_headers):
        """Test /git/status with short flag."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="M file.py", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "M file.py", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/status", json={"short": True}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "status", "--short"]

    def test_git_diff_endpoint(self, client, auth_headers):
        """Test /git/diff endpoint."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/diff", json={}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "diff"]

    def test_git_diff_cached_endpoint(self, client, auth_headers):
        """Test /git/diff with cached flag."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="staged", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "staged", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/diff", json={"cached": True}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "diff", "--cached"]

    def test_git_diff_with_path_endpoint(self, client, auth_headers):
        """Test /git/diff with path."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="diff", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "diff", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/diff", json={"path": "README.md"}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "diff", "--", "README.md"]

    def test_git_log_endpoint(self, client, auth_headers):
        """Test /git/log endpoint."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="abc1234 commit", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "abc1234 commit", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/log", json={}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "log", "--max-count=10", "--oneline"]

    def test_git_commit_endpoint(self, client, auth_headers):
        """Test /git/commit endpoint."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="1 file changed", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "1 file changed", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/commit", json={"message": "Fix bug"}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "commit", "-m", "Fix bug"]

    def test_git_commit_all_endpoint(self, client, auth_headers):
        """Test /git/commit with -a flag."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="committed", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "committed", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/commit", json={"message": "Update", "all": True}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "commit", "-a", "-m", "Update"]

    def test_git_branch_endpoint(self, client, auth_headers):
        """Test /git/branch endpoint."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="* main", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "* main", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/branch", json={}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "branch"]

    def test_git_branch_create_endpoint(self, client, auth_headers):
        """Test /git/branch create."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/branch", json={"name": "feature/new"}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "branch", "feature/new"]

    def test_git_branch_delete_endpoint(self, client, auth_headers):
        """Test /git/branch delete."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="Deleted", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "Deleted", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/branch", json={"name": "old", "delete": True}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "branch", "-d", "old"]

    def test_git_push_endpoint(self, client, auth_headers):
        """Test /git/push endpoint."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="pushed", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "pushed", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/push", json={}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "push", "origin"]

    def test_git_push_with_branch_endpoint(self, client, auth_headers):
        """Test /git/push with branch."""
        with patch("bridge.server.run_command") as mock_run:
            mock_run.return_value = MagicMock(
                ok=True, output="pushed", error="",
                execution_id="test-id",
                model_dump=lambda: {"ok": True, "output": "pushed", "error": "", "execution_id": "test-id"}
            )
            response = client.post("/git/push", json={"branch": "main", "set_upstream": True}, headers=auth_headers)

        assert response.status_code == 200
        args = mock_run.call_args
        assert args[0][0] == ["git", "push", "-u", "origin", "main"]

    def test_git_endpoint_unauthorized(self, client):
        """Test git endpoint without auth returns 403."""
        response = client.post("/git/status", json={})
        assert response.status_code in (401, 403)

    def test_git_endpoint_wrong_token(self, client):
        """Test git endpoint with wrong token."""
        response = client.post(
            "/git/status", json={},
            headers={"Authorization": "Bearer wrong-token"}
        )
        assert response.status_code == 401
