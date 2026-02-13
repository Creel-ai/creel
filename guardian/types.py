"""Guardian types — data classes and Pydantic config models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


class ActionVerdict(str, enum.Enum):
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

    enabled: bool = False  # off by default — adds latency/cost
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 256
    timeout: float = 3.0


class PolicyConfig(BaseModel):
    """Configuration for YAML-based policy engine."""

    enabled: bool = True
    policy_file: str = "policies/default.yaml"


class AuditConfig(BaseModel):
    """Configuration for the JSONL audit logger."""

    enabled: bool = True
    log_file: str = "guardian_audit.jsonl"


class GuardianConfig(BaseModel):
    """Top-level guardian configuration."""

    enabled: bool = True
    fast_classifier: FastClassifierConfig = Field(default_factory=FastClassifierConfig)
    llm_judge: LLMJudgeConfig = Field(default_factory=LLMJudgeConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
