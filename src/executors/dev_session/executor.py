"""Dev session executor — containerized dev environment with process management.

Provides three tool functions dispatched through DevSessionManager:
- dev_exec: spawn commands (foreground or background) inside a dev container
- dev_process: manage running processes (log, poll, write, kill)
- dev_sessions: list all active sessions
"""

from __future__ import annotations


def register_skill():
    """Register the dev_session skill with the skill registry."""
    from creel.skills.models import Param, SkillMeta, ToolSpec

    meta = SkillMeta(
        id="dev_session",
        label="Dev Session",
        tools=(
            ToolSpec(
                name="dev_exec",
                description=(
                    "Run a command in an isolated dev container (foreground or background)"
                ),
                params=(
                    Param(
                        name="command",
                        type="string",
                        description="Shell command to execute",
                        required=True,
                    ),
                    Param(
                        name="background",
                        type="string",
                        description=(
                            "Run in background and return session info (true/false, default false)"
                        ),
                    ),
                    Param(
                        name="workdir",
                        type="string",
                        description="Working directory inside the container",
                    ),
                    Param(
                        name="timeout",
                        type="string",
                        description="Timeout in seconds (default 300)",
                    ),
                ),
                fixed_args={"_action": "exec"},
            ),
            ToolSpec(
                name="dev_process",
                description=(
                    "Manage a running background process in the dev container "
                    "(log, poll, write, kill)"
                ),
                params=(
                    Param(
                        name="session_id",
                        type="string",
                        description="Session identifier",
                        required=True,
                    ),
                    Param(
                        name="action",
                        type="string",
                        description="Action to perform: log, poll, write, or kill",
                    ),
                    Param(
                        name="limit",
                        type="string",
                        description="Max log lines to return (default 100)",
                    ),
                    Param(
                        name="offset",
                        type="string",
                        description="Log line offset (default 0)",
                    ),
                    Param(
                        name="data",
                        type="string",
                        description="Data to write to stdin (for write action)",
                    ),
                ),
                fixed_args={"_action": "process"},
            ),
            ToolSpec(
                name="dev_sessions",
                description="List all active sessions in the dev container",
                params=(),
                fixed_args={"_action": "sessions"},
            ),
        ),
        needs_network=True,
    )

    def execute(config):
        # Container mode dispatch goes through DevSessionManager in tools.py.
        # This execute function is only called for inline mode, which is not
        # supported for dev_session.
        raise RuntimeError(
            "dev_session requires container mode — dispatch goes through DevSessionManager"
        )

    return meta, execute
