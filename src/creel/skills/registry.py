"""Skill registry — discovers and manages skill plugins.

Mirrors the ChannelRegistry pattern (src/creel/channels/registry.py).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import sys
from dataclasses import dataclass

from creel.skills.models import ExecuteFn, SkillMeta, ToolSpec

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "creel.skills"

# Built-in tool names that must not be shadowed by skill plugins.
_BUILTIN_TOOL_NAMES = frozenset(
    {
        "remember",
        "update_long_term_memory",
        "search_memory",
        "delete_memory",
        "edit_memory",
        "list_memory_files",
        "set_workspace",
        "cron",
        "subagent",
        "kb_search",
        "kb_add",
        "kb_list",
        "kb_stats",
    }
)


@dataclass
class SkillEntry:
    """Internal record for a registered skill."""

    meta: SkillMeta
    execute: ExecuteFn


class _LazyExecute:
    """Defers executor module imports until the first tool call.

    At discovery time we only call register_skill() to get metadata.
    The actual execute function (which may import heavy dependencies)
    is loaded on first invocation.
    """

    __slots__ = ("_module_path", "_real_execute")

    def __init__(self, module_path: str) -> None:
        self._module_path = module_path
        self._real_execute: ExecuteFn | None = None

    def __call__(self, config):  # noqa: ANN001
        if self._real_execute is None:
            mod = importlib.import_module(self._module_path)
            _, self._real_execute = mod.register_skill()
        return self._real_execute(config)


# Module-level singleton for sharing a single registry across the process.
_global_registry: SkillRegistry | None = None


def get_shared_registry() -> SkillRegistry:
    """Return (and lazily create) the process-wide shared SkillRegistry."""
    global _global_registry  # noqa: PLW0603
    if _global_registry is None:
        _global_registry = SkillRegistry()
        _global_registry.discover()
    return _global_registry


def reset_shared_registry() -> None:
    """Reset the shared registry (for testing)."""
    global _global_registry  # noqa: PLW0603
    _global_registry = None


class SkillRegistry:
    """Discovers, registers, and looks up skill plugins."""

    def __init__(self) -> None:
        self._entries: dict[str, SkillEntry] = {}  # skill_id -> entry
        self._tool_map: dict[str, str] = {}  # tool_name -> skill_id

    def register(self, meta: SkillMeta, execute: ExecuteFn) -> None:
        """Register a skill by its metadata and execute function.

        Raises ValueError on tool name collision with a different skill.
        """
        # Check for built-in tool name shadowing
        for tool in meta.tools:
            if tool.name in _BUILTIN_TOOL_NAMES:
                raise ValueError(
                    f"Tool name '{tool.name}' is a built-in tool and cannot "
                    f"be registered by skill '{meta.id}'"
                )

        # Check for tool name collisions with other skills
        for tool in meta.tools:
            existing_skill = self._tool_map.get(tool.name)
            if existing_skill is not None and existing_skill != meta.id:
                raise ValueError(
                    f"Tool name '{tool.name}' already registered by skill "
                    f"'{existing_skill}', cannot register for skill '{meta.id}'"
                )

        if meta.id in self._entries:
            # Re-registering the same skill — clear old tool mappings first
            old_meta = self._entries[meta.id].meta
            for tool in old_meta.tools:
                self._tool_map.pop(tool.name, None)
            logger.warning("Overwriting skill '%s'", meta.id)

        self._entries[meta.id] = SkillEntry(meta=meta, execute=execute)
        for tool in meta.tools:
            self._tool_map[tool.name] = meta.id
        logger.info("Registered skill '%s' (%d tools)", meta.id, len(meta.tools))

    # Built-in executor modules to scan as fallback when entry points
    # are unavailable (e.g. PYTHONPATH-based dev setups).
    _BUILTIN_EXECUTORS: list[str] = [
        "executors.weather.executor",
        "executors.gcal.executor",
        "executors.gcal_write.executor",
        "executors.gmail_readonly.executor",
        "executors.gmail_send.executor",
        "executors.gmail_modify.executor",
        "executors.drive.executor",
        "executors.drive_write.executor",
        "executors.google_docs.executor",
        "executors.google_sheets.executor",
        "executors.google_slides.executor",
        "executors.apple_notes.executor",
        "executors.apple_reminders.executor",
        "executors.brave_search.executor",
        "executors.notion.executor",
        "executors.notion_write.executor",
        "executors.fetch_url.executor",
        "executors.browser.executor",
        "executors.exec.executor",
        "executors.exec_interactive.executor",
        "executors.file_ops.executor",
        "executors.github.executor",
        "executors.coding.executor",
        "executors.tts.executor",
        "executors.bluebubbles.executor",
        "executors.things.executor",
        "executors.git_ops.executor",
        "executors.imessage_bridge.executor",
        "executors.host_exec.executor",
    ]

    def discover(self) -> None:
        """Scan for skill plugins and register them.

        Discovery order:
        1. Entry points (pip-installed packages)
        2. Built-in executor modules (fallback)
        3. User directory (~/.creel/skills/) — future extension
        """
        # Phase 1: Entry points
        eps = importlib.metadata.entry_points()
        if hasattr(eps, "select"):
            skill_eps = list(eps.select(group=ENTRY_POINT_GROUP))
        else:
            skill_eps = list(eps.get(ENTRY_POINT_GROUP) or [])

        for ep in skill_eps:
            try:
                register_fn = ep.load()
                meta, execute = register_fn()
                self.register(meta, execute)
            except Exception:
                logger.debug(
                    "Failed to load skill '%s' from entry point",
                    ep.name,
                    exc_info=True,
                )

        # Phase 2: Built-in executor modules
        self._discover_builtins()

        if self._entries:
            logger.info(
                "Skill discovery complete: %s",
                ", ".join(sorted(self._entries.keys())),
            )
        else:
            logger.warning("Skill discovery found no plugins")

    def _discover_builtins(self) -> None:
        """Import built-in executor modules directly to fill any gaps.

        Uses lazy execute wrappers so heavy dependencies (google-api-client,
        beautifulsoup4, etc.) are only imported when a tool is actually called,
        not at discovery time.
        """
        for module_path in self._BUILTIN_EXECUTORS:
            try:
                mod = importlib.import_module(module_path)
                register_fn = getattr(mod, "register_skill", None)
                if register_fn is None:
                    continue
                meta, execute = register_fn()
                if meta.id not in self._entries:
                    # Wrap in a lazy proxy that defers heavy imports to first call
                    lazy_exec = _LazyExecute(module_path)
                    # Store the eagerly-loaded meta but lazy execute
                    self.register(meta, lazy_exec)
            except Exception:
                logger.debug("Could not load built-in skill from %s", module_path)

    def get_skill(self, skill_id: str) -> SkillEntry | None:
        """Look up a skill entry by skill ID."""
        return self._entries.get(skill_id)

    def get_tool(self, tool_name: str) -> tuple[ToolSpec, SkillEntry] | None:
        """Look up a tool by its LLM-visible name.

        Returns (ToolSpec, SkillEntry) or None if not found.
        """
        skill_id = self._tool_map.get(tool_name)
        if skill_id is None:
            return None
        entry = self._entries[skill_id]
        for tool in entry.meta.tools:
            if tool.name == tool_name:
                return tool, entry
        return None  # pragma: no cover — shouldn't happen if maps are consistent

    def all_skills(self) -> list[SkillMeta]:
        """Return metadata for all registered skills compatible with this platform."""
        platform = sys.platform
        result = []
        for entry in self._entries.values():
            if entry.meta.platform is None or entry.meta.platform == platform:
                result.append(entry.meta)
        result.sort(key=lambda m: m.id)
        return result

    def all_tool_names(self) -> list[str]:
        """Return all registered tool names."""
        return sorted(self._tool_map.keys())

    def skill_for_tool(self, tool_name: str) -> str | None:
        """Return the skill ID that provides a given tool, or None."""
        return self._tool_map.get(tool_name)
