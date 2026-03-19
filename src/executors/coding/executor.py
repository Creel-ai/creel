#!/usr/bin/env python3
"""Coding executor — development environment for building software projects.

Runs shell commands in a dev container (or inline via subprocess) with support
for mounting host directories, configurable working directories, and timeouts.

Environment Variables:
    COMMAND: Shell command to execute (e.g., 'python -m pytest', 'npm install')
    WORKDIR: Working directory inside the container (default: /workspace)
    MOUNT: Host path to mount into the container at /workspace
    TIMEOUT: Command timeout in seconds (default: 300)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def register_skill():
    """Register the coding skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="coding",
        label="Coding",
        tools=(
            ToolSpec(
                name="coding",
                description="Run a shell command in a development environment",
                params=(
                    Param(
                        name="command",
                        type="string",
                        description="Shell command to execute",
                        required=True,
                    ),
                    Param(
                        name="workdir",
                        type="string",
                        description="Working directory for command execution",
                    ),
                    Param(
                        name="mount",
                        type="string",
                        description="Host path to mount into the container at /workspace",
                    ),
                    Param(
                        name="timeout",
                        type="string",
                        description="Command timeout in seconds (default: 300, max: 1800)",
                    ),
                ),
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        command = config.args.get("command", "")
        if not command:
            raise ValueError("coding requires a 'command' argument")
        workdir = config.args.get("workdir") or None
        mount = config.args.get("mount") or None
        timeout = None
        timeout_str = config.args.get("timeout")
        if timeout_str:
            timeout = int(timeout_str)
        result = run_command(command, workdir=workdir, mount=mount, timeout=timeout)
        return json.dumps(result, indent=2)

    return meta, execute


# Default timeout: 5 minutes
DEFAULT_TIMEOUT = 300

# Maximum timeout: 30 minutes
MAX_TIMEOUT = 1800

# Paths that must never be mounted (resolved via realpath before checking)
BLOCKED_MOUNT_PATHS = frozenset(
    {
        "/",
        "/etc",
        "/var",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/boot",
        "/dev",
        "/proc",
        "/sys",
        "/root",
        "/System",
        "/Library",
        "/private",
        "/Applications",
    }
)

# Patterns that are always blocked (destructive / exfiltration)
BLOCKED_PATTERNS = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b.*\s+/(\s|$)"),  # rm -rf /
    re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b.*\s+/(\s|$)"),  # rm -fr /
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\b.*\bof=/dev/"),
    # Reverse shell patterns
    re.compile(r">/dev/tcp/"),
    re.compile(r"/dev/udp/"),
    re.compile(r"\bbash\s+-i\b"),
    re.compile(r"\bnc\s+-e\b"),
    re.compile(r"\bncat\s+-e\b"),
    # Pipe to shell (download-and-execute)
    re.compile(r"\bcurl\b.*\|\s*\b(ba|z)?sh\b"),
    re.compile(r"\bwget\b.*\|\s*\b(ba|z)?sh\b"),
    # Command substitution from network
    re.compile(r"\$\(curl\b"),
    re.compile(r"\$\(wget\b"),
    # Fork bomb
    re.compile(r":\(\)\s*\{.*\}"),
    # Crontab modification
    re.compile(r"\bcrontab\b"),
    # chmod 777
    re.compile(r"\bchmod\s+(-R\s+)?777\b"),
]


def _error_result(error: str, *, command: str = "", **extra) -> dict:
    """Build a standard error-result dict."""
    return {
        "command": command,
        "exit_code": -1,
        "stdout": "",
        "stderr": error,
        "error": error,
        "success": False,
        **extra,
    }


def validate_mount_path(path: str) -> str | None:
    """Validate a host mount path for safety.

    Returns None if the path is safe, or an error message if blocked.
    """
    if not path:
        return "Empty mount path"

    # Expand ~ and resolve to absolute path
    resolved = os.path.realpath(os.path.expanduser(path))

    # Check against blocked paths (exact match only — realpath already resolved symlinks)
    for blocked in BLOCKED_MOUNT_PATHS:
        if resolved == blocked:
            return f"Blocked: cannot mount system path '{resolved}'"

    # Verify the path exists
    if not os.path.exists(resolved):
        return f"Mount path does not exist: '{resolved}'"

    # Verify it's a directory
    if not os.path.isdir(resolved):
        return f"Mount path is not a directory: '{resolved}'"

    return None


def validate_command(command: str) -> str | None:
    """Validate a command against the security blocklist.

    Returns None if the command is allowed, or an error message if blocked.
    """
    command = command.strip()
    if not command:
        return "Empty command"

    for pattern in BLOCKED_PATTERNS:
        if pattern.search(command):
            return "Blocked: command matches a dangerous pattern"

    return None


_SETUP_DONE_MARKER = ".creel-setup-done"

# Track which workspaces have been set up in this process (for dev_runner keepalive)
_setup_cache: set[str] = set()


def detect_and_setup(workdir: str) -> dict:
    """Detect project type and install dependencies automatically.

    Looks for common project manifest files and runs the appropriate
    install command. Idempotent — tracks completion via a marker file.

    Args:
        workdir: Absolute path to the project directory.

    Returns:
        Dict describing what was detected, installed, and any errors.
    """
    result: dict = {"workdir": workdir, "detected": None, "installed": False, "error": None}

    marker = os.path.join(workdir, _SETUP_DONE_MARKER)
    if os.path.exists(marker) or workdir in _setup_cache:
        result["installed"] = True
        result["detected"] = "already-setup"
        return result

    # Detection order matters: more specific first
    detectors = [
        ("package.json", "npm ci --prefer-offline 2>&1 || npm install 2>&1"),
        (
            "requirements.txt",
            "uv pip install --system -r requirements.txt 2>&1 || pip install -r requirements.txt 2>&1",
        ),
        ("pyproject.toml", "uv pip install --system -e . 2>&1 || pip install -e . 2>&1"),
        ("Cargo.toml", "cargo build 2>&1"),
        ("go.mod", "go mod download 2>&1"),
    ]

    detected_file: str | None = None
    install_cmd: str | None = None
    for manifest, cmd in detectors:
        if os.path.exists(os.path.join(workdir, manifest)):
            detected_file = manifest
            install_cmd = cmd
            break

    if not detected_file or not install_cmd:
        # No known project type — mark as done to avoid re-scanning
        _setup_cache.add(workdir)
        return result

    result["detected"] = detected_file
    assert install_cmd is not None  # guaranteed by the loop above

    try:
        proc = subprocess.run(
            ["bash", "-c", install_cmd],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode == 0:
            result["installed"] = True
            # Write marker file
            try:
                Path(marker).write_text(f"setup via {detected_file}\n")
            except OSError:
                pass
            _setup_cache.add(workdir)
        else:
            result["error"] = proc.stderr[:500] if proc.stderr else f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        result["error"] = "Setup timed out after 300s"
    except Exception as e:
        result["error"] = str(e)

    return result


def run_command(
    command: str,
    workdir: str | None = None,
    mount: str | None = None,
    timeout: int | None = None,
) -> dict:
    """Run a shell command and return structured results.

    Args:
        command: Shell command to execute
        workdir: Working directory for command execution
        mount: Host path to mount (in container mode, bind-mounted to /workspace)
        timeout: Command timeout in seconds (default: 300, max: 1800)

    Returns:
        Dict with stdout, stderr, exit_code, command, workdir, and success
    """
    # Validate command against security rules
    error = validate_command(command)
    if error:
        return _error_result(error, command=command)

    # Validate and resolve mount path if provided
    if mount:
        mount_error = validate_mount_path(mount)
        if mount_error:
            return _error_result(mount_error, command=command)
        # Resolve mount to absolute path
        mount = os.path.realpath(os.path.expanduser(mount))

    # Determine effective working directory
    effective_workdir = workdir
    if not effective_workdir and mount:
        effective_workdir = mount

    # Validate workdir if provided
    if effective_workdir:
        workdir_path = Path(effective_workdir)
        if not workdir_path.is_absolute():
            if mount:
                workdir_path = Path(mount) / workdir_path
            else:
                workdir_path = Path.cwd() / workdir_path

        if not workdir_path.exists():
            return _error_result(
                f"Working directory does not exist: {workdir_path}",
                command=command,
                workdir=str(workdir_path),
            )

        if not workdir_path.is_dir():
            return _error_result(
                f"Working directory is not a directory: {workdir_path}",
                command=command,
                workdir=str(workdir_path),
            )

        effective_workdir = str(workdir_path)

    # Resolve timeout
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    try:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=effective_workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return {
            "command": command,
            "workdir": effective_workdir,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.returncode == 0,
        }

    except subprocess.TimeoutExpired as e:
        return _error_result(
            f"Command timed out after {timeout} seconds",
            command=command,
            workdir=effective_workdir,
            stdout=e.stdout.decode() if e.stdout else "",
            stderr=e.stderr.decode() if e.stderr else "",
        )
    except Exception as e:
        return _error_result(
            f"Execution failed: {e}",
            command=command,
            workdir=effective_workdir,
        )


def main() -> None:
    """Main entry point — reads parameters from env vars or CLI args."""
    command = os.environ.get("COMMAND", "")
    workdir = os.environ.get("WORKDIR") or None
    mount = os.environ.get("MOUNT") or None
    timeout_str = os.environ.get("TIMEOUT") or None

    # Also accept as CLI args
    if len(sys.argv) > 1:
        command = sys.argv[1]
    if len(sys.argv) > 2:
        workdir = sys.argv[2]
    if len(sys.argv) > 3:
        mount = sys.argv[3]

    if not command:
        result = _error_result(
            "COMMAND environment variable or first argument required",
            exit_code=1,
        )
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)

    timeout = None
    if timeout_str:
        try:
            timeout = int(timeout_str)
        except ValueError:
            pass

    try:
        result = run_command(command, workdir=workdir, mount=mount, timeout=timeout)
        print(json.dumps(result, indent=2))
        # Always exit 0 — the JSON result carries the command's exit code.
        # A non-zero inner command is not an executor infrastructure failure.

    except Exception as e:
        result = _error_result(str(e), command=command, exit_code=1)
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
