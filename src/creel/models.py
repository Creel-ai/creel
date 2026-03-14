"""Task definition models with validation."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from guardian.types import GuardianConfig

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_HARD_LIMIT = 120.0


@dataclass
class SessionState:
    """Per-sender state carried across the agent loop and tool execution.

    Replaces the untyped ``dict`` previously threaded through the system.
    """

    sender_id: str = ""
    workspace: str | None = None
    model_override: str | None = None


class HttpConfig(BaseModel):
    """HTTP request timeout and limit configuration for network executors."""

    timeout: float = Field(default=15.0, gt=0, description="Total request timeout in seconds")
    connect_timeout: float = Field(default=5.0, gt=0, description="Connection timeout in seconds")
    max_redirects: int = Field(default=3, ge=0, description="Maximum number of redirects to follow")
    max_size_mb: float = Field(default=5.0, gt=0, description="Maximum response size in MB")

    @field_validator("timeout")
    @classmethod
    def clamp_timeout(cls, v: float) -> float:
        """Enforce hard upper limit of 120 seconds."""
        if v > _HTTP_TIMEOUT_HARD_LIMIT:
            logger.warning(
                "timeout %ss exceeds hard limit, clamped to %ss", v, _HTTP_TIMEOUT_HARD_LIMIT
            )
            return _HTTP_TIMEOUT_HARD_LIMIT
        return v

    @field_validator("connect_timeout")
    @classmethod
    def clamp_connect_timeout(cls, v: float) -> float:
        """Enforce hard upper limit of 120 seconds."""
        if v > _HTTP_TIMEOUT_HARD_LIMIT:
            logger.warning(
                "connect_timeout %ss exceeds hard limit, clamped to %ss",
                v,
                _HTTP_TIMEOUT_HARD_LIMIT,
            )
            return _HTTP_TIMEOUT_HARD_LIMIT
        return v


class ExecutorConfig(BaseModel):
    """Configuration for a single executor step."""

    name: str = ""
    secrets: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    timeout: int = 60  # seconds, per-executor configurable
    http: HttpConfig = Field(default_factory=HttpConfig)

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


class RateLimitConfig(BaseModel):
    """Rate limiting configuration for LLM API calls."""

    requests_per_minute: int = Field(default=30, ge=1)
    requests_per_hour: int = Field(default=500, ge=1)
    tokens_per_day: int = Field(default=1_000_000, ge=1)
    cost_per_day_usd: float = Field(default=10.00, gt=0)
    queue_timeout: float = Field(default=30.0, ge=0)
    enabled: bool = False


class LLMConfig(BaseModel):
    """Configuration for the LLM processing step."""

    provider: str = "anthropic"
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 300
    secrets: str | None = None
    api_base: str | None = None  # Custom endpoint (e.g. Ollama, proxies)
    region: str | None = None  # AWS region for Bedrock
    fallback: list[str] = Field(
        default_factory=list,
        description="Failover chain of 'provider/model' strings tried on transient errors",
    )
    container_pool: ContainerPoolConfig = Field(default_factory=ContainerPoolConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)


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
    host_auth: bool = Field(
        default=False, description="Mount host CLI auth into executor container (read-only)"
    )
    # Per-executor container resource overrides
    writable: bool = Field(default=False, description="Skip --read-only when True")
    tmpfs_size: str = Field(default="16M", description="Size of /tmp tmpfs mount")
    memory: str = Field(default="256m", description="Docker --memory limit")
    cpus: str = Field(default="0.5", description="Docker --cpus limit")
    timeout: int = Field(default=60, ge=1, description="Executor timeout in seconds")
    cache_ttl: int = Field(default=0, ge=0, description="Cache TTL in seconds (0 = no caching)")
    http: HttpConfig = Field(default_factory=HttpConfig, description="HTTP request timeouts")

    @field_validator("tmpfs_size")
    @classmethod
    def validate_tmpfs_size(cls, v: str) -> str:
        """Validate Docker size format (e.g. '16M', '256M', '1G')."""
        import re

        if not re.fullmatch(r"\d+[kKmMgG]", v):
            raise ValueError(f"tmpfs_size must be a Docker size like '16M' or '1G', got '{v}'")
        return v

    @field_validator("memory")
    @classmethod
    def validate_memory(cls, v: str) -> str:
        """Validate Docker memory format (e.g. '256m', '1g')."""
        import re

        if not re.fullmatch(r"\d+[kKmMgG]", v):
            raise ValueError(f"memory must be a Docker size like '256m' or '1g', got '{v}'")
        return v

    @field_validator("cpus")
    @classmethod
    def validate_cpus(cls, v: str) -> str:
        """Validate cpus is a positive float string."""
        try:
            val = float(v)
        except ValueError as e:
            raise ValueError(f"cpus must be a positive number string, got '{v}'") from e
        if val <= 0:
            raise ValueError(f"cpus must be positive, got '{v}'")
        return v


class AgentConfig(BaseModel):
    """Agent loop settings."""

    max_turns: int = Field(default=10, ge=1, le=50)


class ToolCacheConfig(BaseModel):
    """Configuration for tool result caching."""

    enabled: bool = False
    default_ttl: int = Field(default=300, ge=0, description="Default TTL in seconds")
    max_entries: int = Field(default=256, ge=1, description="Maximum cache entries")
    tool_ttls: dict[str, int] = Field(
        default_factory=dict,
        description="Per-tool TTL overrides in seconds (e.g. {'check_weather': 1800})",
    )


class ContextPruningConfig(BaseModel):
    """Configuration for context window pruning."""

    enabled: bool = False
    threshold: float = Field(
        default=0.80,
        gt=0,
        le=1.0,
        description="Fraction of max_context_tokens at which pruning activates",
    )
    min_recent_messages: int = Field(
        default=4, ge=1, description="Minimum recent messages to always keep"
    )


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
    tool_cache: ToolCacheConfig = Field(default_factory=ToolCacheConfig)
    context_pruning: ContextPruningConfig = Field(default_factory=ContextPruningConfig)
    model_override: str | None = None  # Per-session "provider/model" override


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
    fts_enabled: bool = True
    recency_half_life_days: float = Field(default=30.0, gt=0)
    compact_summarize: bool = True
    compact_model: str = "claude-haiku-4-5-20251001"
    compact_max_tokens: int = Field(default=512, gt=0)
    memory_context_mode: Literal["recent", "relevant"] = "recent"
    memory_context_max_results: int = 20
    extra_paths: list[str] = Field(default_factory=list)
    index_session_transcripts: bool = False


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
