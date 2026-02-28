#!/usr/bin/env python3
"""GitHub CLI executor — wraps `gh` for managing issues, PRs, CI runs, and code search.

Runs gh CLI commands via subprocess. Authenticates via GH_TOKEN environment
variable or existing `gh auth login` state.

Environment Variables:
    COMMAND: The gh CLI subcommand to run (e.g., 'issue list', 'pr view 42')
    REPO: Optional repository in owner/repo format
    GH_TOKEN: GitHub personal access token (injected from agent config secrets)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

# Subcommands that are always allowed (read-only operations)
ALLOWED_SUBCOMMANDS = frozenset(
    {
        "issue list",
        "issue view",
        "pr list",
        "pr view",
        "run list",
        "run view",
        "run watch",
        "search code",
        "search issues",
        "search prs",
    }
)

# Subcommands that require review (write operations — Guardian policy gates these)
REVIEW_SUBCOMMANDS = frozenset(
    {
        "issue create",
        "issue comment",
        "issue close",
        "issue reopen",
        "pr create",
        "pr comment",
        "pr merge",
        "pr close",
        "pr reopen",
    }
)

# Patterns that are always blocked (destructive operations)
BLOCKED_PATTERNS = [
    re.compile(r"^repo\s+delete\b"),
    re.compile(r"^issue\s+delete\b"),
    re.compile(r"^pr\s+merge\s+.*--admin\b"),
    # Block api calls with destructive HTTP methods
    re.compile(r"^api\s+.*(-X|--method)\s+(DELETE|PUT|PATCH)\b", re.IGNORECASE),
]

# All valid top-level subcommand prefixes
VALID_PREFIXES = frozenset({"issue", "pr", "run", "search", "api"})


def _parse_subcommand(command: str) -> str:
    """Extract the two-word gh subcommand from the command string.

    Examples:
        'issue list --state open' -> 'issue list'
        'pr view 42'             -> 'pr view'
        'api /repos/...'         -> 'api'
    """
    parts = command.strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    if len(parts) == 1:
        return parts[0]
    return ""


def validate_command(command: str) -> str | None:
    """Validate a gh command against the security allowlist.

    Returns None if the command is allowed, or an error message if blocked.
    """
    command = command.strip()
    if not command:
        return "Empty command"

    # Check blocked patterns first (hard deny)
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(command):
            return "Blocked: command matches a destructive pattern"

    # Validate top-level subcommand
    top_level = command.split()[0]
    if top_level not in VALID_PREFIXES:
        return (
            f"Unknown gh subcommand: '{top_level}'. "
            f"Allowed: {', '.join(sorted(VALID_PREFIXES))}"
        )

    # 'api' subcommand: allow by default (GET); DELETE/PUT already blocked above
    if top_level == "api":
        return None

    # Check the two-word subcommand against allowed + review lists
    subcommand = _parse_subcommand(command)
    if subcommand in ALLOWED_SUBCOMMANDS or subcommand in REVIEW_SUBCOMMANDS:
        return None

    return f"Subcommand '{subcommand}' is not in the allowed list"


def build_gh_command(command: str, repo: str | None = None) -> list[str]:
    """Build the full gh CLI argument list.

    Args:
        command: The gh subcommand string (e.g., 'issue list --state open')
        repo: Optional repository in owner/repo format

    Returns:
        List of command arguments for subprocess.run
    """
    cmd = ["gh"] + command.strip().split()

    if repo:
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            raise ValueError(f"Invalid repo format: '{repo}'. Expected owner/repo")
        cmd.extend(["--repo", repo])

    return cmd


def run_gh_command(command: str, repo: str | None = None) -> dict:
    """Run a gh CLI command and return structured results.

    Args:
        command: The gh subcommand string (e.g., 'issue list', 'pr view 42')
        repo: Optional repository in owner/repo format

    Returns:
        Dict with stdout, stderr, exit_code, success, and command info
    """
    # Check gh is installed
    if not shutil.which("gh"):
        return {
            "error": "gh CLI is not installed. Install from https://cli.github.com/",
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "gh: command not found",
        }

    # Validate command against security rules
    error = validate_command(command)
    if error:
        return {
            "error": error,
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": error,
            "command": command,
        }

    # Build the full command
    try:
        cmd = build_gh_command(command, repo)
    except ValueError as e:
        return {
            "error": str(e),
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
            "command": command,
        }

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,  # 2 minute timeout
        )

        return {
            "command": " ".join(cmd),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.returncode == 0,
        }

    except subprocess.TimeoutExpired as e:
        return {
            "command": " ".join(cmd),
            "exit_code": -1,
            "stdout": e.stdout.decode() if e.stdout else "",
            "stderr": e.stderr.decode() if e.stderr else "",
            "error": "Command timed out after 2 minutes",
            "success": False,
        }
    except Exception as e:
        return {
            "command": command,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
            "error": f"Execution failed: {e}",
            "success": False,
        }


def main() -> None:
    """Main entry point — reads parameters from env vars or CLI args."""
    command = os.environ.get("COMMAND", "")
    repo = os.environ.get("REPO") or None

    # Also accept as CLI args
    if len(sys.argv) > 1:
        command = sys.argv[1]
    if len(sys.argv) > 2:
        repo = sys.argv[2]

    if not command:
        result = {
            "error": "No command provided",
            "success": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "COMMAND environment variable or first argument required",
        }
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)

    result = run_gh_command(command, repo)

    if result.get("error") or not result.get("success"):
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
