"""Destructive command blocklist — safety floor that runs before Guardian.

Provides a hardcoded set of dangerous command patterns plus user-configurable
custom patterns and allowlist.  The check runs only for host-exec executors
(tools that execute unsandboxed commands on the host machine).

Matched commands are never auto-approved — they always require explicit
human confirmation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from creel.models import DestructiveBlocklistConfig, ToolConfig

logger = logging.getLogger(__name__)

# Executor names that run unsandboxed on the host.
_HOST_EXEC_TOOLS: frozenset[str] = frozenset({"exec", "host_exec", "exec_interactive", "coding"})

# Built-in patterns reused from bridge/process_manager.py and
# exec_interactive/executor.py, plus additional patterns from issue #250.
_BUILTIN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # rm with recursive+force in any flag style (-rf, -fr, --recursive, --force)
    (
        "rm_recursive_force",
        re.compile(r"\brm\s+.*(?:-\w*r\w*f|-\w*f\w*r|--recursive|--force)", re.IGNORECASE),
    ),
    # rm with separated short flags: rm -r -f, rm -f -r
    (
        "rm_separated_flags",
        re.compile(r"\brm\s+.*-r\b.*-f\b|\brm\s+.*-f\b.*-r\b", re.IGNORECASE),
    ),
    ("mkfs", re.compile(r"\bmkfs\b")),
    ("dd_to_device", re.compile(r"\bdd\b.*\bof\s*=\s*/dev/")),
    (
        "write_to_block_device",
        re.compile(r">\s*/dev/sd[a-z]|>\s*/dev/nvme"),
    ),
    (
        "fork_bomb",
        re.compile(r":\(\)\s*\{.*:\s*\|\s*:.*&.*\}\s*;\s*:"),
    ),
    ("chmod_777_root", re.compile(r"\bchmod\s+.*777\s+/")),
    (
        "chown_recursive_system",
        re.compile(r"\bchown\s+.*-R\s+.*\s+/(?:etc|usr|var|bin|sbin|lib|boot)\b"),
    ),
    ("curl_pipe_sh", re.compile(r"\bcurl\b.*\|\s*(?:ba)?sh\b")),
    ("wget_pipe_sh", re.compile(r"\bwget\b.*\|\s*(?:ba)?sh\b")),
    ("reverse_shell", re.compile(r"/dev/tcp/|/dev/udp/")),
    ("bind_shell", re.compile(r"\bnc\b.*-[el]|\bncat\b.*-[el]")),
    ("sudo", re.compile(r"\bsudo\b")),
    (
        "su_root",
        re.compile(r"(?:^|\s|[;&|])su\s+-?\s*root\b|(?:^|\s|[;&|])su\s*$"),
    ),
    # Additional patterns from issue #250
    (
        "sql_ddl_drop",
        re.compile(r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),
    ),
    (
        "sql_ddl_truncate",
        re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    ),
    ("terraform_destroy", re.compile(r"\bterraform\s+destroy\b")),
    (
        "kubectl_delete_namespace",
        re.compile(r"\bkubectl\s+delete\s+namespace\b"),
    ),
    ("shutdown", re.compile(r"\bshutdown\b")),
    ("reboot", re.compile(r"\breboot\b")),
    ("kill_pid_1", re.compile(r"\bkill\s+.*-9\s+1\b|\bkill\s+-9\s+1\b")),
]


@dataclass(frozen=True, slots=True)
class BlocklistMatch:
    """Result of a destructive command blocklist check."""

    matched: bool
    pattern_name: str
    command: str


def check_destructive_blocklist(
    tool_name: str,
    tool_input: dict,
    tools_config: dict[str, ToolConfig],
    config: DestructiveBlocklistConfig,
) -> BlocklistMatch | None:
    """Check whether a tool call matches the destructive command blocklist.

    Returns a ``BlocklistMatch`` if the command matches a blocked pattern,
    or ``None`` if the command is safe / not applicable.

    Only tools whose executor is in ``_HOST_EXEC_TOOLS`` are checked.
    """
    # Look up executor — skip non-host-exec tools
    tool_cfg = tools_config.get(tool_name)
    if tool_cfg is None:
        return None
    if tool_cfg.executor not in _HOST_EXEC_TOOLS:
        return None

    # Extract the command string from tool input
    command = tool_input.get("command") or tool_input.get("input") or tool_input.get("data") or ""
    if not isinstance(command, str) or not command.strip():
        return None

    # Allowlist: exact substring match bypasses the blocklist
    for allowed in config.allowlist:
        if allowed and allowed in command:
            logger.debug("Blocklist allowlisted: %r matches allowlist entry %r", command, allowed)
            return None

    # Check built-in patterns
    for name, pattern in _BUILTIN_PATTERNS:
        if pattern.search(command):
            logger.warning("Blocklist match: pattern=%s tool=%s", name, tool_name)
            return BlocklistMatch(matched=True, pattern_name=name, command=command)

    # Check custom patterns
    for raw in config.custom_patterns:
        try:
            compiled = re.compile(raw, re.IGNORECASE)
        except re.error:
            logger.warning("Invalid custom blocklist pattern: %r", raw)
            continue
        if compiled.search(command):
            logger.warning("Blocklist match (custom): pattern=%r tool=%s", raw, tool_name)
            return BlocklistMatch(matched=True, pattern_name=f"custom:{raw}", command=command)

    return None
