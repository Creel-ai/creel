"""Central path resolution for Creel.

Resolution order: explicit CLI arg > CREEL_HOME env var > ~/.creel/
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def creel_home() -> Path:
    """Return the Creel home directory (~/.creel/ by default)."""
    return Path(os.environ.get("CREEL_HOME", str(Path.home() / ".creel")))


def agent_config() -> Path:
    return creel_home() / "agent.yaml"


def policies_dir() -> Path:
    return creel_home() / "policies"


def secrets_dir() -> Path:
    return creel_home() / "secrets"


def sessions_dir() -> Path:
    return creel_home() / "sessions"


def workspace_dir() -> Path:
    return creel_home() / "workspace"


def tasks_dir() -> Path:
    return creel_home() / "tasks"


def cron_dir() -> Path:
    return creel_home() / "cron"


def deployments_dir() -> Path:
    return creel_home() / "deployments"


def audit_log() -> Path:
    return creel_home() / "guardian_audit.jsonl"


def is_initialized() -> bool:
    return agent_config().exists()


def creel_executable() -> str | None:
    """Path to ``creel`` console script (pipx/venv), or None to fall back to -m."""
    return shutil.which("creel")
