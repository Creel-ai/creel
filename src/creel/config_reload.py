"""Hot-reload support for agent configuration.

Provides config diffing, validation, and atomic application so the daemon
can pick up agent.yaml changes without a restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from creel.models import AgentDefinition, load_agent_config

logger = logging.getLogger(__name__)

# Fields that require a full daemon restart — changing these at runtime is unsafe
# because they affect socket binding, database connections, or process-level state.
NON_RELOADABLE_FIELDS = frozenset(
    {
        "session.sessions_dir",
    }
)

# Top-level fields that are safe to reload at runtime.
RELOADABLE_FIELDS = frozenset(
    {
        "system_prompt",
        "system_prompt_file",
        "tools",
        "llm",
        "agent",
        "session",
        "workspace",
        "channels",
        "quiet_hours",
        "bridge",
        "browser",
        "media",
        "guardian",
    }
)


@dataclass
class ConfigChange:
    """A single configuration field that changed."""

    field: str
    old_value: Any = None
    new_value: Any = None

    def __str__(self) -> str:
        return f"{self.field}: {_summarize(self.old_value)} -> {_summarize(self.new_value)}"


@dataclass
class ReloadResult:
    """Result of a config reload attempt."""

    success: bool
    changes: list[ConfigChange] = field(default_factory=list)
    non_reloadable: list[ConfigChange] = field(default_factory=list)
    error: str | None = None
    new_config: AgentDefinition | None = None

    @property
    def changed_count(self) -> int:
        return len(self.changes)

    def summary(self) -> str:
        if self.error:
            return f"Reload failed: {self.error}"
        if not self.changes and not self.non_reloadable:
            return "No config changes detected."
        parts = []
        if self.changes:
            parts.append(f"{len(self.changes)} setting(s) updated")
        if self.non_reloadable:
            fields = ", ".join(c.field for c in self.non_reloadable)
            parts.append(f"{len(self.non_reloadable)} non-reloadable change(s) ignored ({fields})")
        return "Config reloaded: " + "; ".join(parts)


def _summarize(value: Any) -> str:
    """Create a short string representation of a config value."""
    if value is None:
        return "null"
    if isinstance(value, str):
        if len(value) > 60:
            return f'"{value[:57]}..."'
        return f'"{value}"'
    if isinstance(value, dict):
        return f"{{{len(value)} items}}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    return str(value)


def diff_configs(
    old: AgentDefinition,
    new: AgentDefinition,
) -> list[ConfigChange]:
    """Compare two AgentDefinition instances and return a list of changes.

    Only compares top-level fields of the model. For nested models, any
    difference in the serialized form counts as a change.
    """
    changes: list[ConfigChange] = []
    old_dict = old.model_dump()
    new_dict = new.model_dump()

    for field_name in RELOADABLE_FIELDS:
        old_val = old_dict.get(field_name)
        new_val = new_dict.get(field_name)
        if old_val != new_val:
            changes.append(
                ConfigChange(
                    field=field_name,
                    old_value=old_val,
                    new_value=new_val,
                )
            )

    return changes


def classify_changes(
    changes: list[ConfigChange],
) -> tuple[list[ConfigChange], list[ConfigChange]]:
    """Split changes into (reloadable, non_reloadable) lists.

    Uses a dotted-path check: a change to "session" is non-reloadable if
    "session.sessions_dir" is in NON_RELOADABLE_FIELDS *and* that specific
    sub-field actually changed.
    """
    reloadable = []
    non_reloadable = []

    for change in changes:
        # Check if any non-reloadable sub-field is affected
        is_non_reloadable = False
        for nr_field in NON_RELOADABLE_FIELDS:
            if nr_field.startswith(change.field + "."):
                # The top-level field changed — check if the specific sub-field changed
                sub_field = nr_field.split(".", 1)[1]
                old_sub = (
                    change.old_value.get(sub_field) if isinstance(change.old_value, dict) else None
                )
                new_sub = (
                    change.new_value.get(sub_field) if isinstance(change.new_value, dict) else None
                )
                if old_sub != new_sub:
                    is_non_reloadable = True
                    non_reloadable.append(
                        ConfigChange(field=nr_field, old_value=old_sub, new_value=new_sub)
                    )
            elif nr_field == change.field:
                is_non_reloadable = True
                non_reloadable.append(change)

        if not is_non_reloadable:
            reloadable.append(change)

    return reloadable, non_reloadable


def reload_from_path(
    config_path: str | Path,
    current: AgentDefinition,
) -> ReloadResult:
    """Load config from disk, validate, diff, and return a ReloadResult.

    Does NOT apply changes — the caller (DaemonService) is responsible
    for atomically swapping the config under its lock.
    """
    config_path = Path(config_path)

    try:
        new_config = load_agent_config(config_path)
    except FileNotFoundError:
        return ReloadResult(success=False, error=f"Config file not found: {config_path}")
    except Exception as exc:
        return ReloadResult(success=False, error=f"Invalid config: {exc}")

    all_changes = diff_configs(current, new_config)
    if not all_changes:
        return ReloadResult(success=True, new_config=new_config)

    reloadable, non_reloadable = classify_changes(all_changes)

    return ReloadResult(
        success=True,
        changes=reloadable,
        non_reloadable=non_reloadable,
        new_config=new_config,
    )
