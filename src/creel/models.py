"""Task definition models with validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class ContainerPoolConfig(BaseModel):
    """Configuration for the warm LLM container pool."""

    enabled: bool = True
    idle_timeout_seconds: int = 300
    max_containers: int = 2


class LLMConfig(BaseModel):
    """Configuration for the LLM processing step."""

    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 300
    secrets: str | None = None
    container_pool: ContainerPoolConfig = Field(default_factory=ContainerPoolConfig)


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
    host_auth: bool = False


class AgentConfig(BaseModel):
    """Agent loop settings."""

    max_turns: int = Field(default=10, ge=1, le=50)


class SessionConfig(BaseModel):
    """Session storage settings."""

    sessions_dir: str = "sessions"
    max_history: int = 50
    summarize_on_trim: bool = True
    ttl_hours: float = 0  # 0 = no expiry
    summary_model: str = "claude-haiku-4-5-20251001"
    summary_max_tokens: int = 1024
    max_context_tokens: int = 180_000
    encryption_key: str | None = None  # Fernet key or passphrase for encryption at rest


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
    memory_max_chars: int = 20_000
    max_chars_per_file: int = 20_000
    compact_after_days: int = 7
    max_daily_entries: int = 50
    max_long_term_lines: int = 500


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


class WhatsAppChannelConfig(BaseModel):
    """WhatsApp channel settings."""

    phone_number: str
    mode: str = "polling"  # "polling" or "webhook"
    auth_state_dir: str = "whatsapp_auth"
    webhook_path: str = "/webhooks/whatsapp"
    webhook_verify_token: str = ""
    webhook_secret: str = ""
    bridge_url: str | None = None
    poll_interval: int = 5
    allowed_senders: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_webhook_verify_token(self) -> WhatsAppChannelConfig:
        if self.mode == "webhook" and not self.webhook_verify_token:
            raise ValueError("webhook_verify_token must be set when mode is 'webhook'")
        return self

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"polling", "webhook"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got '{v}'")
        return v

    @field_validator("phone_number")
    @classmethod
    def expand_phone(cls, v: str) -> str:
        return os.path.expandvars(v)

    @field_validator("allowed_senders", mode="before")
    @classmethod
    def expand_allowed_senders(cls, v: list[str] | str) -> list[str]:
        if isinstance(v, str):
            v = [v]
        return [os.path.expandvars(s) for s in v]


class TelegramChannelConfig(BaseModel):
    """Telegram Bot API channel settings."""

    bot_token: str = "$TELEGRAM_BOT_TOKEN"
    secrets: str | None = None
    mode: str = "polling"  # "polling" or "webhook"
    poll_timeout: int = 30
    webhook_path: str = "/webhooks/telegram"
    webhook_secret: str = ""
    allowed_senders: list[str] = Field(default_factory=list)
    allowed_chats: list[str] = Field(default_factory=list)
    send_typing: bool = True
    api_base_url: str | None = None  # Custom Bot API server (e.g. local test server)

    @model_validator(mode="after")
    def check_allowed_senders_required(self) -> TelegramChannelConfig:
        if not self.allowed_senders:
            raise ValueError(
                "allowed_senders must not be empty — Telegram channel requires an explicit allow list"
            )
        return self

    @model_validator(mode="after")
    def check_webhook_secret(self) -> TelegramChannelConfig:
        if self.mode == "webhook" and not self.webhook_secret:
            raise ValueError("webhook_secret must be set when mode is 'webhook'")
        return self

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"polling", "webhook"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got '{v}'")
        return v

    @field_validator("bot_token")
    @classmethod
    def expand_bot_token(cls, v: str) -> str:
        return os.path.expandvars(v)

    @field_validator("allowed_senders", mode="before")
    @classmethod
    def expand_allowed_senders(cls, v: list[str] | str) -> list[str]:
        if isinstance(v, str):
            v = [v]
        return [os.path.expandvars(s) for s in v]

    @field_validator("allowed_chats", mode="before")
    @classmethod
    def expand_allowed_chats(cls, v: list[str] | str) -> list[str]:
        if isinstance(v, str):
            v = [v]
        return [os.path.expandvars(s) for s in v]


class TranscriptionConfig(BaseModel):
    """Transcription backend settings (media.transcription)."""

    backend: str = "openai"  # "openai" or "local"
    model: str = "whisper-1"
    api_key: str | None = None  # Falls back to llm.api_key / OPENAI_API_KEY


class VisionConfig(BaseModel):
    """Vision processing settings (media.vision)."""

    max_pixels: int = 2048
    quality: int = 85


class MediaConfig(BaseModel):
    """Media attachment handling settings.

    Controls image and voice message processing.  When *enabled* is
    ``False`` (or the section is absent), attachments are silently
    ignored and only the text portion of messages is processed.
    """

    enabled: bool = True
    storage_dir: str = "~/.creel/media"
    max_file_size_mb: int = 20
    retention_days: int = 30
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)


class BridgeConfig(BaseModel):
    """Bridge server configuration for host-side macOS tools."""

    url: str = "http://localhost:8766"
    token: str | None = None
    enabled: bool = False


class BrowserConfig(BaseModel):
    """Browser tool configuration for web browsing via Playwright CDP."""

    enabled: bool = False
    default_mode: str = "managed"  # "managed" | "relay" | "native"
    cdp_url: str | None = None  # Chrome CDP endpoint for relay mode
    max_sessions: int = 3
    session_timeout_minutes: int = 10
    headless: bool = True
    blocked_domains: list[str] = Field(default_factory=list)
    container_memory: str = "1024m"
    container_shm_size: str = "256m"
    container_tmpfs_size: str = "128M"
    navigate_timeout_ms: int = 30000
    snapshot_timeout_ms: int = 15000
    block_heavy_resources: bool = True


class ChannelsConfig(BaseModel):
    """All channel configurations."""

    model_config = ConfigDict(extra="allow")

    imessage: IMessageChannelConfig | None = None
    bluebubbles: BlueBubblesChannelConfig | None = None
    whatsapp: WhatsAppChannelConfig | None = None
    telegram: TelegramChannelConfig | None = None

    def configured_channels(self) -> list[str]:
        """Return IDs of channels that have configuration present."""
        result = []
        for name, _field_info in self.model_fields.items():
            val = getattr(self, name, None)
            if val is not None and isinstance(val, BaseModel):
                result.append(name)
        # Check extra fields for plugin-provided channels
        if self.model_extra:
            for key, val in self.model_extra.items():
                if val is not None and key not in result:
                    result.append(key)
        return result

    def get_channel_config(self, channel_id: str) -> dict[str, Any] | None:
        """Return the raw config dict for a channel, or None if unconfigured."""
        typed = getattr(self, channel_id, None)
        if typed is not None and isinstance(typed, BaseModel):
            return typed.model_dump()
        # Check extras
        if self.model_extra and channel_id in self.model_extra:
            val = self.model_extra[channel_id]
            if isinstance(val, dict):
                return val
        return None


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
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    media: MediaConfig | None = None
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
    allowed_tools: list[str] = Field(default_factory=list)

    @field_validator("schedule")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        parts = v.split()
        if len(parts) != 5:
            raise ValueError(f"schedule must be a 5-part cron expression, got {len(parts)} parts")
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


def load_agent_config(path: str | Path | None = None) -> AgentDefinition:
    """Load the global agent configuration from a YAML file."""
    if path is None:
        from creel import paths

        path = paths.agent_config()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Agent config not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Agent config must contain a YAML mapping, got {type(raw)}")

    return AgentDefinition(**raw)
