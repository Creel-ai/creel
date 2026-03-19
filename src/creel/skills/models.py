"""Skill data models — intrinsic metadata that each executor carries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creel.models import ExecutorConfig


@dataclass(frozen=True)
class Param:
    """A single parameter exposed to the LLM for a tool."""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """One LLM-visible tool provided by a skill."""

    name: str
    description: str
    params: tuple[Param, ...] = ()
    fixed_args: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillMeta:
    """Intrinsic metadata for a skill (executor).

    This is what each executor knows about itself — tool names, descriptions,
    parameters, and runtime requirements.  Deployment-specific config (secrets,
    resource limits) lives in ``SkillOverride`` in agent.yaml.
    """

    id: str  # e.g. "weather", "gmail_modify"
    label: str  # Human-readable name
    tools: tuple[ToolSpec, ...]  # One or more tools this skill provides
    needs_network: bool = False
    needs_bridge: bool = False
    bridge_scope: str | None = None  # e.g. "THINGS", "GIT", "NOTES"
    platform: str | None = None  # e.g. "darwin" for macOS-only


# The execute function signature: takes an ExecutorConfig, returns a string result.
ExecuteFn = Callable[["ExecutorConfig"], str]
