"""Tests for the github executor."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from executors.github.executor import (
    ALLOWED_SUBCOMMANDS,
    DEFAULT_MAX_CHARS,
    REVIEW_SUBCOMMANDS,
    build_gh_command,
    run_gh_command,
    validate_command,
)


class TestValidateCommand:
    """Tests for command validation against the security allowlist."""

    def test_allowed_read_commands(self) -> None:
        """Test that all read-only subcommands are allowed."""
        for subcmd in ALLOWED_SUBCOMMANDS:
            assert validate_command(subcmd) is None, f"Should allow: {subcmd}"

    def test_allowed_read_commands_with_args(self) -> None:
        """Test allowed commands with additional arguments."""
        assert validate_command("issue list --state open") is None
        assert validate_command("pr view 42") is None
        assert validate_command("run list --limit 10") is None
        assert validate_command("search code 'def main'") is None
        assert validate_command("search issues 'bug fix'") is None

    def test_review_commands_allowed(self) -> None:
        """Test that review subcommands pass validation (Guardian gates them)."""
        for subcmd in REVIEW_SUBCOMMANDS:
            assert validate_command(subcmd) is None, f"Should allow (for review): {subcmd}"

    def test_review_commands_with_args(self) -> None:
        """Test review commands with additional arguments."""
        assert validate_command("issue create --title 'Bug report'") is None
        assert validate_command("pr create --title 'Fix' --body 'Details'") is None
        assert validate_command("issue comment 42 --body 'Looks good'") is None
        assert validate_command("pr merge 42") is None
        assert validate_command("issue close 42") is None

    def test_api_get_allowed(self) -> None:
        """Test that api subcommand is allowed (defaults to GET)."""
        assert validate_command("api /repos/owner/repo/issues") is None
        assert validate_command("api /repos/owner/repo/pulls --paginate") is None

    def test_blocked_repo_delete(self) -> None:
        """Test that repo delete is blocked."""
        result = validate_command("repo delete owner/repo")
        assert result is not None
        assert "destructive" in result.lower()

    def test_blocked_issue_delete(self) -> None:
        """Test that issue delete is blocked."""
        result = validate_command("issue delete 42")
        assert result is not None
        assert "destructive" in result.lower()

    def test_blocked_pr_merge_admin(self) -> None:
        """Test that pr merge --admin is blocked."""
        result = validate_command("pr merge 42 --admin")
        assert result is not None
        assert "destructive" in result.lower()

    def test_blocked_api_delete(self) -> None:
        """Test that api with DELETE method is blocked."""
        result = validate_command("api /repos/owner/repo -X DELETE")
        assert result is not None
        assert "destructive" in result.lower()

    def test_blocked_api_put(self) -> None:
        """Test that api with PUT method is blocked."""
        result = validate_command("api /repos/owner/repo --method PUT")
        assert result is not None
        assert "destructive" in result.lower()

    def test_blocked_api_delete_case_insensitive(self) -> None:
        """Test that api DELETE blocking is case-insensitive."""
        result = validate_command("api /repos/owner/repo -X delete")
        assert result is not None

    def test_unknown_subcommand_blocked(self) -> None:
        """Test that unknown top-level subcommands are blocked."""
        result = validate_command("release create v1.0")
        assert result is not None
        assert "Unknown gh subcommand" in result

    def test_unknown_action_blocked(self) -> None:
        """Test that unknown two-word subcommands are blocked."""
        result = validate_command("issue transfer 42 other/repo")
        assert result is not None
        assert "not in the allowed list" in result

    def test_empty_command(self) -> None:
        """Test that empty command is rejected."""
        assert validate_command("") is not None
        assert validate_command("   ") is not None


class TestBuildGhCommand:
    """Tests for gh command construction."""

    def test_simple_command(self) -> None:
        """Test building a simple command."""
        cmd = build_gh_command("issue list")
        assert cmd == ["gh", "issue", "list"]

    def test_command_with_args(self) -> None:
        """Test building a command with arguments."""
        cmd = build_gh_command("pr view 42 --comments")
        assert cmd == ["gh", "pr", "view", "42", "--comments"]

    def test_command_with_repo(self) -> None:
        """Test that --repo flag is appended when repo is provided."""
        cmd = build_gh_command("issue list", repo="owner/repo")
        assert cmd == ["gh", "issue", "list", "--repo", "owner/repo"]

    def test_command_with_repo_and_args(self) -> None:
        """Test command with both args and repo flag."""
        cmd = build_gh_command("pr list --state open", repo="owner/repo")
        assert cmd == ["gh", "pr", "list", "--state", "open", "--repo", "owner/repo"]

    def test_repo_none_no_flag(self) -> None:
        """Test that no --repo flag is added when repo is None."""
        cmd = build_gh_command("issue list", repo=None)
        assert "--repo" not in cmd

    def test_invalid_repo_format(self) -> None:
        """Test that invalid repo format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid repo format"):
            build_gh_command("issue list", repo="not-a-valid-repo")

    def test_invalid_repo_with_spaces(self) -> None:
        """Test that repo with spaces is rejected."""
        with pytest.raises(ValueError, match="Invalid repo format"):
            build_gh_command("issue list", repo="owner /repo")

    def test_valid_repo_with_dots(self) -> None:
        """Test that repo names with dots are accepted."""
        cmd = build_gh_command("issue list", repo="owner/my.repo")
        assert cmd == ["gh", "issue", "list", "--repo", "owner/my.repo"]

    def test_valid_repo_with_hyphens(self) -> None:
        """Test that repo names with hyphens are accepted."""
        cmd = build_gh_command("issue list", repo="my-org/my-repo")
        assert cmd == ["gh", "issue", "list", "--repo", "my-org/my-repo"]

    def test_whitespace_stripped(self) -> None:
        """Test that leading/trailing whitespace is stripped."""
        cmd = build_gh_command("  issue list  ")
        assert cmd == ["gh", "issue", "list"]


class TestRunGhCommand:
    """Tests for running gh commands via subprocess."""

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_successful_command(self, mock_run, mock_which) -> None:
        """Test a successful gh command execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="#1 Bug report\n#2 Feature request\n",
            stderr="",
        )

        result = run_gh_command("issue list")

        assert result["success"] is True
        assert result["exit_code"] == 0
        assert "#1 Bug report" in result["stdout"]
        mock_run.assert_called_once_with(
            ["gh", "issue", "list"],
            capture_output=True,
            text=True,
            timeout=120,
        )

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_command_with_repo_flag(self, mock_run, mock_which) -> None:
        """Test command execution with repo flag."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="PR #42: Fix bug",
            stderr="",
        )

        result = run_gh_command("pr view 42", repo="owner/repo")

        assert result["success"] is True
        mock_run.assert_called_once_with(
            ["gh", "pr", "view", "42", "--repo", "owner/repo"],
            capture_output=True,
            text=True,
            timeout=120,
        )

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_command_failure(self, mock_run, mock_which) -> None:
        """Test handling of a failed gh command."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="GraphQL: Could not resolve to a Repository",
        )

        result = run_gh_command("issue list", repo="nonexistent/repo")

        assert result["success"] is False
        assert result["exit_code"] == 1
        assert "Could not resolve" in result["stderr"]

    @patch("shutil.which", return_value=None)
    def test_gh_not_installed(self, mock_which) -> None:
        """Test error when gh CLI is not installed."""
        result = run_gh_command("issue list")

        assert result["success"] is False
        assert "not installed" in result["error"]
        assert result["exit_code"] == -1

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    def test_blocked_command_not_executed(self, mock_which) -> None:
        """Test that blocked commands are never executed."""
        with patch("subprocess.run") as mock_run:
            result = run_gh_command("repo delete owner/repo")

            assert result["success"] is False
            assert "destructive" in result["error"].lower()
            mock_run.assert_not_called()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    def test_invalid_repo_format_error(self, mock_which) -> None:
        """Test error when repo format is invalid."""
        result = run_gh_command("issue list", repo="bad-format")

        assert result["success"] is False
        assert "Invalid repo format" in result["error"]

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_command_timeout(self, mock_run, mock_which) -> None:
        """Test handling of command timeout."""
        timeout_exception = subprocess.TimeoutExpired(
            cmd=["gh", "run", "watch", "12345"],
            timeout=120,
        )
        timeout_exception.stdout = b"partial output"
        timeout_exception.stderr = b""
        mock_run.side_effect = timeout_exception

        result = run_gh_command("run watch 12345")

        assert result["success"] is False
        assert result["exit_code"] == -1
        assert "timed out" in result["error"]
        assert result["stdout"] == "partial output"

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_auth_failure(self, mock_run, mock_which) -> None:
        """Test handling of authentication failure."""
        mock_run.return_value = MagicMock(
            returncode=4,
            stdout="",
            stderr="gh: To use GitHub CLI in a non-interactive context, set the GH_TOKEN environment variable.",
        )

        result = run_gh_command("issue list")

        assert result["success"] is False
        assert result["exit_code"] == 4
        assert "GH_TOKEN" in result["stderr"]

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    def test_unknown_subcommand_not_executed(self, mock_which) -> None:
        """Test that unknown subcommands are never executed."""
        with patch("subprocess.run") as mock_run:
            result = run_gh_command("codespace create")

            assert result["success"] is False
            assert "Unknown gh subcommand" in result["error"]
            mock_run.assert_not_called()


class TestSecurityRules:
    """Tests for security rule enforcement."""

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    def test_api_delete_blocked(self, mock_which) -> None:
        """Test that API DELETE requests are blocked."""
        with patch("subprocess.run") as mock_run:
            result = run_gh_command("api /repos/owner/repo/issues/1 -X DELETE")
            assert result["success"] is False
            mock_run.assert_not_called()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    def test_api_put_blocked(self, mock_which) -> None:
        """Test that API PUT requests are blocked."""
        with patch("subprocess.run") as mock_run:
            result = run_gh_command("api /repos/owner/repo --method PUT -f data=value")
            assert result["success"] is False
            mock_run.assert_not_called()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_api_get_allowed(self, mock_run, mock_which) -> None:
        """Test that API GET requests are allowed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"data": "response"}',
            stderr="",
        )

        result = run_gh_command("api /repos/owner/repo/issues")
        assert result["success"] is True
        mock_run.assert_called_once()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_api_post_allowed(self, mock_run, mock_which) -> None:
        """Test that API POST requests are allowed (not in blocked list)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id": 1}',
            stderr="",
        )

        result = run_gh_command("api /repos/owner/repo/issues -X POST -f title=test")
        assert result["success"] is True
        mock_run.assert_called_once()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    def test_pr_merge_admin_blocked(self, mock_which) -> None:
        """Test that pr merge --admin is blocked."""
        with patch("subprocess.run") as mock_run:
            result = run_gh_command("pr merge 42 --admin")
            assert result["success"] is False
            mock_run.assert_not_called()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    def test_api_patch_blocked(self, mock_which) -> None:
        """Test that API PATCH requests are blocked (always a mutation)."""
        with patch("subprocess.run") as mock_run:
            result = run_gh_command("api /repos/owner/repo/issues/1 -X PATCH -f state=closed")
            assert result["success"] is False
            assert "destructive" in result["error"].lower()
            mock_run.assert_not_called()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    def test_api_patch_method_flag_blocked(self, mock_which) -> None:
        """Test that API PATCH via --method flag is blocked."""
        with patch("subprocess.run") as mock_run:
            result = run_gh_command("api /repos/owner/repo/pulls/1 --method PATCH -f title=new")
            assert result["success"] is False
            mock_run.assert_not_called()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_api_post_passes_executor(self, mock_run, mock_which) -> None:
        """Test that API POST passes executor validation (policy gates it separately)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id": 1}',
            stderr="",
        )
        result = run_gh_command("api /repos/owner/repo/issues -X POST -f title=test")
        assert result["success"] is True
        mock_run.assert_called_once()

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_pr_merge_without_admin_allowed(self, mock_run, mock_which) -> None:
        """Test that regular pr merge is allowed (Guardian gates it)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Merged PR #42",
            stderr="",
        )

        result = run_gh_command("pr merge 42")
        assert result["success"] is True


class TestOutputTruncation:
    """Tests for stdout size limiting."""

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_output_truncated_when_exceeds_max_chars(self, mock_run, mock_which) -> None:
        """Test that large stdout is truncated and flagged."""
        big_output = "x" * (DEFAULT_MAX_CHARS + 1000)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=big_output,
            stderr="",
        )

        result = run_gh_command("issue list")

        assert result["success"] is True
        assert len(result["stdout"]) == DEFAULT_MAX_CHARS
        assert result["truncated"] is True

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    def test_output_not_truncated_when_within_limit(self, mock_run, mock_which) -> None:
        """Test that output within the limit is not truncated."""
        small_output = "x" * 100
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=small_output,
            stderr="",
        )

        result = run_gh_command("issue list")

        assert result["success"] is True
        assert result["stdout"] == small_output
        assert "truncated" not in result

    @patch("shutil.which", return_value="/usr/local/bin/gh")
    @patch("subprocess.run")
    @patch.dict("os.environ", {"MAX_CHARS": "50"})
    def test_max_chars_env_override(self, mock_run, mock_which) -> None:
        """Test that MAX_CHARS env var overrides the default limit."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="x" * 100,
            stderr="",
        )

        result = run_gh_command("issue list")

        assert result["success"] is True
        assert len(result["stdout"]) == 50
        assert result["truncated"] is True


class TestMainFunction:
    """Tests for the main executor entry point."""

    @patch("executors.github.executor.run_gh_command")
    @patch("builtins.print")
    def test_main_success(self, mock_print, mock_run) -> None:
        """Test main with a successful command."""
        mock_run.return_value = {
            "success": True,
            "exit_code": 0,
            "stdout": "output",
            "stderr": "",
            "command": "gh issue list",
        }

        import os
        import sys

        with patch.dict(os.environ, {"COMMAND": "issue list"}), patch.object(
            sys, "argv", ["executor.py"]
        ):
            from executors.github.executor import main

            main()

        mock_run.assert_called_once_with("issue list", None)

    @patch("executors.github.executor.run_gh_command")
    @patch("builtins.print")
    def test_main_with_repo(self, mock_print, mock_run) -> None:
        """Test main with REPO env var."""
        mock_run.return_value = {
            "success": True,
            "exit_code": 0,
            "stdout": "output",
            "stderr": "",
            "command": "gh issue list --repo owner/repo",
        }

        import os
        import sys

        with patch.dict(os.environ, {"COMMAND": "issue list", "REPO": "owner/repo"}), patch.object(
            sys, "argv", ["executor.py"]
        ):
            from executors.github.executor import main

            main()

        mock_run.assert_called_once_with("issue list", "owner/repo")

    @patch("builtins.print")
    def test_main_no_command(self, mock_print) -> None:
        """Test main with missing command."""
        import os
        import sys

        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["executor.py"]):
            with pytest.raises(SystemExit) as excinfo:
                from executors.github.executor import main

                main()

        assert excinfo.value.code == 1

    @patch("executors.github.executor.run_gh_command")
    @patch("builtins.print")
    def test_main_error_exits_nonzero(self, mock_print, mock_run) -> None:
        """Test main exits with code 1 on error."""
        mock_run.return_value = {
            "success": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "error",
            "error": "Command failed",
        }

        import os
        import sys

        with patch.dict(os.environ, {"COMMAND": "issue list"}), patch.object(
            sys, "argv", ["executor.py"]
        ):
            with pytest.raises(SystemExit) as excinfo:
                from executors.github.executor import main

                main()

        assert excinfo.value.code == 1
