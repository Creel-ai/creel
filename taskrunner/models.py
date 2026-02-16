"""Task definition models with validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from guardian.types import GuardianConfig


class ExecutorConfig(BaseModel):
    """Configuration for a single executor step."""

    name: str = ""
    secrets: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    timeout: int = 60  # seconds, per-executor configurable

    @property
    def image(self) -> str:
        """Derive Docker image name from executor name by convention."""
        return f"executor-{self.name.replace('_', '-')}:latest"


class OutputConfig(BaseModel):
    """Configuration for task output routing."""

    type: str
    to: str

    @field_validator("type")
    @classmethod
    def validate_output_type(cls, v: str) -> str:
        allowed = {"imessage", "stdout", "file"}
        if v not in allowed:
            raise ValueError(f"output type must be one of {allowed}, got '{v}'")
        return v

    @field_validator("to")
    @classmethod
    def expand_env_vars(cls, v: str) -> str:
        return os.path.expandvars(v)


class LLMConfig(BaseModel):
    """Configuration for the LLM processing step."""

    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 300
    secrets: str | None = None


# --- Agent / tool models ---


class MountConfig(BaseModel):
    """Configuration for mounting a host path into an executor container."""
    
    path: str
    mode: str = Field(default="ro", pattern="^(ro|rw)$")


class ToolParameter(BaseModel):
    """A single parameter exposed to the LLM for a tool."""

    type: str = "string"
    description: str = ""
    required: bool = False


class ToolConfig(BaseModel):
    """Configuration for one tool available to the agent."""

    executor: str
    secrets: str | None = None
    description: str
    parameters: dict[str, ToolParameter] = Field(default_factory=dict)
    fixed_args: dict[str, str] = Field(default_factory=dict)
    classify_output: bool = False
    mounts: list[MountConfig] = Field(default_factory=list)
    network: bool = False
    image: str | None = None


class AgentConfig(BaseModel):
    """Agent loop settings."""

    max_turns: int = Field(default=10, ge=1, le=50)


class SessionConfig(BaseModel):
    """Session storage settings."""

    sessions_dir: str = "sessions"
    max_history: int = 50
    summarize_on_trim: bool = True
    ttl_hours: float = 0  # 0 = no expiry


class QuietHoursConfig(BaseModel):
    """Quiet hours configuration to suppress proactive notifications during specified times."""

    enabled: bool = False
    start: str = "23:00"  # 24h format
    end: str = "08:00"
    timezone: str = "UTC"
    allow_urgent: bool = True  # still allow messages marked urgent


class WorkspaceConfig(BaseModel):
    """Workspace directory settings for personality/memory files."""

    path: str = "workspace"
    timezone: str = "UTC"
    memory_days: int = 2
    memory_max_chars: int = 5000
    max_chars_per_file: int = 20_000
    compact_after_days: int = 7
    max_daily_entries: int = 50
    max_long_term_lines: int = 200


class IMessageChannelConfig(BaseModel):
    """iMessage channel settings."""

    listen_to: str
    poll_interval: int = 3

    @field_validator("listen_to")
    @classmethod
    def expand_env_vars(cls, v: str) -> str:
        return os.path.expandvars(v)


class BlueBubblesChannelConfig(BaseModel):
    """BlueBubbles channel settings."""

    server_url: str
    password: str = ""
    listen_to: list[str] = Field(default_factory=list)
    poll_interval: int = 3

    @field_validator("server_url")
    @classmethod
    def expand_server_url(cls, v: str) -> str:
        return os.path.expandvars(v)

    @field_validator("password")
    @classmethod
    def expand_password(cls, v: str) -> str:
        return os.path.expandvars(v)

    @field_validator("listen_to", mode="before")
    @classmethod
    def expand_listen_to(cls, v: list[str] | str) -> list[str]:
        if isinstance(v, str):
            v = [v]
        return [os.path.expandvars(s) for s in v]


class BridgeConfig(BaseModel):
    """Bridge server configuration for host-side macOS tools."""
    
    url: str = "http://localhost:8766"
    token: str | None = None
    enabled: bool = False


class ChannelsConfig(BaseModel):
    """All channel configurations."""

    imessage: IMessageChannelConfig | None = None
    bluebubbles: BlueBubblesChannelConfig | None = None


class AgentDefinition(BaseModel):
    """Global agent config loaded from agent.yaml."""

    system_prompt: str
    system_prompt_file: str | None = None
    tools: dict[str, ToolConfig] = Field(default_factory=dict)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    guardian: GuardianConfig | None = None


# --- Task definition ---


class TaskDefinition(BaseModel):
    """A complete task definition parsed from YAML."""

    name: str
    schedule: str
    executors: dict[str, ExecutorConfig] = Field(default_factory=dict)
    prompt: str
    output: OutputConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mode: str = "simple"
    tools: dict[str, ToolConfig] = Field(default_factory=dict)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    @field_validator("schedule")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        parts = v.split()
        if len(parts) != 5:
            raise ValueError(
                f"schedule must be a 5-part cron expression, got {len(parts)} parts"
            )
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"simple", "agent"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got '{v}'")
        return v


def load_task(path: str | Path) -> TaskDefinition:
    """Load and validate a task definition from a YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Task file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Task file must contain a YAML mapping, got {type(raw)}")

    task = TaskDefinition(**raw)
    for executor_name, executor_cfg in task.executors.items():
        if not executor_cfg.name:
            executor_cfg.name = executor_name
    return task


def load_all_tasks(tasks_dir: str | Path = "tasks") -> list[TaskDefinition]:
    """Load all task definitions from a directory."""
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"Tasks directory not found: {tasks_dir}")

    tasks = []
    for path in sorted(tasks_dir.glob("*.yaml")):
        tasks.append(load_task(path))
    return tasks


def load_agent_config(path: str | Path = "agent.yaml") -> AgentDefinition:
    """Load the global agent configuration from a YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Agent config not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Agent config must contain a YAML mapping, got {type(raw)}")

    return AgentDefinition(**raw)
