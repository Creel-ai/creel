"""Task definition models with validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class FetcherConfig(BaseModel):
    """Configuration for a single fetcher step."""

    image: str
    secrets: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


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


class TaskDefinition(BaseModel):
    """A complete task definition parsed from YAML."""

    name: str
    schedule: str
    fetch: dict[str, FetcherConfig]
    prompt: str
    output: OutputConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @field_validator("schedule")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        parts = v.split()
        if len(parts) != 5:
            raise ValueError(
                f"schedule must be a 5-part cron expression, got {len(parts)} parts"
            )
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

    return TaskDefinition(**raw)


def load_all_tasks(tasks_dir: str | Path = "tasks") -> list[TaskDefinition]:
    """Load all task definitions from a directory."""
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"Tasks directory not found: {tasks_dir}")

    tasks = []
    for path in sorted(tasks_dir.glob("*.yaml")):
        tasks.append(load_task(path))
    return tasks
