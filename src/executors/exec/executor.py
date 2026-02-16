#!/usr/bin/env python3
"""Generic exec executor - runs shell commands via bash.

Takes a command string and executes it safely in a controlled environment.
Outputs JSON with stdout, stderr, and exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run_command(command: str, workdir: str | None = None) -> dict:
    """Run a shell command via bash and return structured results.
    
    Args:
        command: Shell command to execute
        workdir: Working directory for command execution
        
    Returns:
        Dict with stdout, stderr, exit_code, and command info
    """
    # Validate workdir if provided
    if workdir:
        workdir_path = Path(workdir)
        if not workdir_path.is_absolute():
            # Resolve relative paths from current working directory
            workdir_path = Path.cwd() / workdir_path
        
        if not workdir_path.exists():
            raise FileNotFoundError(f"Working directory does not exist: {workdir_path}")
        
        if not workdir_path.is_dir():
            raise NotADirectoryError(f"Working directory is not a directory: {workdir_path}")
        
        workdir = str(workdir_path)

    try:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )
        
        return {
            "command": command,
            "workdir": workdir,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.returncode == 0,
        }
        
    except subprocess.TimeoutExpired as e:
        return {
            "command": command,
            "workdir": workdir,
            "exit_code": -1,
            "stdout": e.stdout.decode() if e.stdout else "",
            "stderr": e.stderr.decode() if e.stderr else "",
            "error": "Command timed out after 5 minutes",
            "success": False,
        }
    except Exception as e:
        return {
            "command": command,
            "workdir": workdir,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
            "error": f"Execution failed: {e}",
            "success": False,
        }


def main() -> None:
    """Main entry point - reads command from env or CLI args."""
    command = os.environ.get("COMMAND", "")
    workdir = os.environ.get("WORKDIR")
    
    # Also accept as CLI args
    if len(sys.argv) > 1:
        command = sys.argv[1]
    if len(sys.argv) > 2:
        workdir = sys.argv[2]
    
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
    
    try:
        result = run_command(command, workdir)
        print(json.dumps(result, indent=2))
        
        # Exit with the original command's exit code
        sys.exit(result["exit_code"])
        
    except Exception as e:
        result = {
            "error": str(e),
            "success": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": str(e),
            "command": command,
            "workdir": workdir,
        }
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()