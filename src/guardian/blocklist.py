"""Destructive command blocklist — hardcoded safety floor.

Regex patterns for known-destructive commands that are ALWAYS denied,
regardless of Guardian policy. This is a non-overridable hard block that
applies only to exec/shell-type tools (exec, host_exec, coding).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Tools whose arguments should be checked against the blocklist.
SHELL_TOOLS = frozenset({"exec", "host_exec", "coding"})

# Argument keys that may contain shell commands.
_COMMAND_ARG_KEYS = ("command", "cmd", "script", "code", "shell_command")


@dataclass
class BlocklistResult:
    """Result of a destructive-command blocklist check."""

    blocked: bool
    pattern_matched: str = ""
    reason: str = ""


# Each entry: (compiled regex, human-readable description).
_DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\brm\s+.*-\s*[^\s]*r[^\s]*f|rm\s+.*-\s*[^\s]*f[^\s]*r", re.IGNORECASE),
        "Recursive force delete (rm -rf)",
    ),
    (
        re.compile(r"\brm\s+(-[^\s]*\s+)*(/|~/?\s)", re.IGNORECASE),
        "Delete root or home directory",
    ),
    (
        re.compile(r"\bgit\s+reset\s+--hard", re.IGNORECASE),
        "Git hard reset (destroys uncommitted changes)",
    ),
    (
        re.compile(r"\bgit\s+push\s+.*--force\b(?!-)|\bgit\s+push\s+-f\b", re.IGNORECASE),
        "Git force push (rewrites remote history)",
    ),
    (
        re.compile(r"\bdrop\s+(table|database|schema|index)\b", re.IGNORECASE),
        "SQL DROP statement (irreversible data loss)",
    ),
    (
        re.compile(r"\btruncate\s+table\b", re.IGNORECASE),
        "SQL TRUNCATE TABLE (irreversible data loss)",
    ),
    (
        re.compile(r"\bmkfs\b", re.IGNORECASE),
        "Format filesystem (mkfs)",
    ),
    (
        re.compile(r"\bdd\s+if=", re.IGNORECASE),
        "Raw disk write (dd)",
    ),
    (
        re.compile(r"\bterraform\s+destroy\b", re.IGNORECASE),
        "Terraform destroy (tears down infrastructure)",
    ),
    (
        re.compile(r"\bkubectl\s+delete\s+(namespace|ns)\b", re.IGNORECASE),
        "Kubectl delete namespace (destroys all resources in namespace)",
    ),
    (
        re.compile(r"\bdocker\s+system\s+prune\b", re.IGNORECASE),
        "Docker system prune (removes all unused data)",
    ),
    (
        re.compile(r"\bchmod\s+(-[^\s]*\s+)*-?\s*[0-7]*777\s+/", re.IGNORECASE),
        "chmod 777 on root paths (dangerous permissions)",
    ),
    (
        re.compile(r"\b(shutdown|poweroff|reboot|init\s+[06])\b", re.IGNORECASE),
        "System shutdown/reboot",
    ),
    (
        re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;?\s*:", re.IGNORECASE),
        "Fork bomb",
    ),
    (
        re.compile(r"\b(nc|ncat|netcat)\b.*\s-[^\s]*e\b", re.IGNORECASE),
        "Reverse shell via netcat",
    ),
    (
        re.compile(r"\bmkdir\s+-p\s+/dev/null\b", re.IGNORECASE),
        "Destructive /dev/null override",
    ),
    (
        re.compile(r">\s*/dev/sda", re.IGNORECASE),
        "Write directly to disk device",
    ),
    (
        re.compile(r"\bwipefs\b", re.IGNORECASE),
        "Wipe filesystem signatures (wipefs)",
    ),
]


def check_blocklist(tool_name: str, tool_input: dict) -> BlocklistResult:
    """Check a tool call against the destructive command blocklist.

    Only inspects exec/shell-type tools. Returns immediately for other tools.

    Args:
        tool_name: The tool being called.
        tool_input: The tool's input arguments dict.

    Returns:
        A BlocklistResult indicating whether the command was blocked.
    """
    if tool_name not in SHELL_TOOLS:
        return BlocklistResult(blocked=False)

    # Gather all string values from known command argument keys.
    commands: list[str] = []
    for key in _COMMAND_ARG_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            commands.append(val)

    if not commands:
        return BlocklistResult(blocked=False)

    combined = "\n".join(commands)

    for pattern, description in _DESTRUCTIVE_PATTERNS:
        if pattern.search(combined):
            logger.warning(
                "Blocklist matched destructive command in %s: %s",
                tool_name,
                description,
            )
            return BlocklistResult(
                blocked=True,
                pattern_matched=pattern.pattern,
                reason=f"Destructive command blocked: {description}",
            )

    return BlocklistResult(blocked=False)
