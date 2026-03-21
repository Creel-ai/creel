"""Guardian types — data classes and Pydantic config models."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from pydantic import BaseModel, Field


class ActionVerdict(enum.StrEnum):
    ALLOW = "allow"
    REVIEW = "review"
    DENY = "deny"


@dataclass
class ClassifierResult:
    """Result from a single classification stage."""

    is_injection: bool
    confidence: float  # 0.0-1.0
    source: str  # "fast_classifier" | "llm_judge"
    reasoning: str = ""


@dataclass
class ScreenResult:
    """Combined result from input screening (stages 1+2)."""

    blocked: bool
    classifier_result: ClassifierResult | None = None
    judge_result: ClassifierResult | None = None
    rejection_message: str = ""


@dataclass
class CoherenceResult:
    """Result from the action coherence check."""

    coherent: bool
    confidence: float  # 0.0-1.0
    reasoning: str = ""


@dataclass
class ActionDecision:
    """Result from policy-based action validation (stage 3)."""

    verdict: ActionVerdict
    tool_name: str
    matched_rule: str = ""
    reason: str = ""


# --- Pydantic config models ---


class FastClassifierConfig(BaseModel):
    """Configuration for the local DeBERTa prompt-injection classifier."""

    enabled: bool = True
    threshold: float = 0.85
    model_name: str = "protectai/deberta-v3-base-prompt-injection-v2"


class LLMJudgeConfig(BaseModel):
    """Configuration for the LLM-based judge (Haiku)."""

    enabled: bool = True  # enabled by default for security
    provider: str | None = None  # None = inherit from main LLM config
    model: str = "claude-haiku-4-5"
    max_tokens: int = 256
    timeout: float = 3.0
    uncertain_only: bool = True  # only run when classifier is uncertain
    uncertain_low: float = 0.5  # lower bound of uncertain range
    uncertain_high: float = 0.85  # upper bound of uncertain range


class PolicyConfig(BaseModel):
    """Configuration for YAML-based policy engine."""

    enabled: bool = True
    policy_file: str = ""  # resolved at runtime via creel.paths


class AuditConfig(BaseModel):
    """Configuration for the JSONL audit logger."""

    enabled: bool = True
    log_file: str = ""  # resolved at runtime via creel.paths
    rotate_daily: bool = False
    max_size_mb: float = 0  # 0 = no size limit


class CoherenceConfig(BaseModel):
    """Configuration for the action coherence checker."""

    enabled: bool = False  # off by default — opt-in
    provider: str | None = None  # None = inherit from main LLM config
    model: str = "claude-haiku-4-5"
    max_tokens: int = 256
    timeout: float = 10.0


class DriftConfig(BaseModel):
    """Configuration for behavioral drift detection."""

    enabled: bool = True
    z_threshold: float = 3.0
    error_threshold: float = 0.10
    error_window_size: int = 100
    new_tool_grace_count: int = 0  # immediate alert, no grace period


class ReviewConfig(BaseModel):
    """Configuration for REVIEW verdict approval flow."""

    timeout_seconds: int = 60
    default_on_timeout: str = "deny"  # "deny" or "allow"
    approvals_dir: str = "approvals"
    max_pending_age_hours: int = 24


class NetworkPolicyConfig(BaseModel):
    """Configuration for network traffic monitoring and control."""

    enabled: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    max_request_size_mb: float = 10.0
    max_response_size_mb: float = 50.0
    rate_limit_per_minute: int = 100
    alert_on_unknown: bool = True


class PipelineConfig(BaseModel):
    """Configuration for parallel/sequential pipeline execution.

    Checks listed in ``parallel_checks`` run concurrently via asyncio.
    Checks in ``sequential_checks`` run one-at-a-time *after* the parallel
    phase completes.  When ``short_circuit`` is True the pipeline cancels
    remaining checks as soon as any check blocks.
    """

    parallel_checks: list[str] = Field(
        default_factory=lambda: [
            "injection_detector",
            "policy_engine",
            "coherence_checker",
        ]
    )
    sequential_checks: list[str] = Field(
        default_factory=lambda: [
            "drift_detector",
        ]
    )
    short_circuit: bool = True
    timeout: float = 5.0  # max seconds for the entire pipeline


class OverrideConfig(BaseModel):
    """Configuration for temporary policy overrides."""

    enabled: bool = True
    absolute_max_duration_hours: float = 24.0
    excluded_tools: list[str] = Field(default_factory=lambda: ["delete_*"])
    require_confirmation_for_wildcard: bool = True
    max_active_overrides: int = 20


class GuardianConfig(BaseModel):
    """Top-level guardian configuration."""

    enabled: bool = True
    debug: bool = False
    fast_classifier: FastClassifierConfig = Field(default_factory=FastClassifierConfig)
    llm_judge: LLMJudgeConfig = Field(default_factory=LLMJudgeConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    coherence: CoherenceConfig = Field(default_factory=CoherenceConfig)
    drift: DriftConfig = Field(default_factory=DriftConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    network_policy: NetworkPolicyConfig = Field(default_factory=NetworkPolicyConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    overrides: OverrideConfig = Field(default_factory=OverrideConfig)
