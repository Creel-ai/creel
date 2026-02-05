"""Tests for task definition loading and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from taskrunner.models import (
    FetcherConfig,
    LLMConfig,
    OutputConfig,
    TaskDefinition,
    load_task,
)


@pytest.fixture
def valid_task_yaml(tmp_path: Path) -> Path:
    """Create a valid task YAML file."""
    task = {
        "name": "test_task",
        "schedule": "0 7 * * *",
        "fetch": {
            "weather": {
                "image": "fetcher-weather:latest",
                "args": {"location": "denver"},
            }
        },
        "prompt": "Today is {date}. Weather: {weather}",
        "output": {"type": "stdout", "to": ""},
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
    }
    path = tmp_path / "test_task.yaml"
    path.write_text(yaml.dump(task))
    return path


def test_load_valid_task(valid_task_yaml: Path) -> None:
    task = load_task(valid_task_yaml)
    assert task.name == "test_task"
    assert task.schedule == "0 7 * * *"
    assert "weather" in task.fetch
    assert task.fetch["weather"].image == "fetcher-weather:latest"
    assert task.output.type == "stdout"
    assert task.llm.model == "claude-sonnet-4-20250514"


def test_load_task_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_task("/nonexistent/path.yaml")


def test_invalid_cron_schedule(tmp_path: Path) -> None:
    task = {
        "name": "bad_cron",
        "schedule": "not a cron",
        "fetch": {},
        "prompt": "test",
        "output": {"type": "stdout", "to": ""},
    }
    path = tmp_path / "bad_cron.yaml"
    path.write_text(yaml.dump(task))
    with pytest.raises(Exception):  # Pydantic ValidationError
        load_task(path)


def test_invalid_output_type(tmp_path: Path) -> None:
    task = {
        "name": "bad_output",
        "schedule": "0 7 * * *",
        "fetch": {},
        "prompt": "test",
        "output": {"type": "telegram", "to": "someone"},
    }
    path = tmp_path / "bad_output.yaml"
    path.write_text(yaml.dump(task))
    with pytest.raises(Exception):
        load_task(path)


def test_default_llm_config(tmp_path: Path) -> None:
    task = {
        "name": "defaults",
        "schedule": "0 7 * * *",
        "fetch": {},
        "prompt": "test",
        "output": {"type": "stdout", "to": ""},
    }
    path = tmp_path / "defaults.yaml"
    path.write_text(yaml.dump(task))
    loaded = load_task(path)
    assert loaded.llm.model == "claude-sonnet-4-20250514"
    assert loaded.llm.max_tokens == 300


def test_fetcher_config_with_secrets() -> None:
    config = FetcherConfig(
        image="fetcher-gcal:latest",
        secrets="secrets/gcal.env.enc",
        args={"range": "today"},
    )
    assert config.secrets == "secrets/gcal.env.enc"
    assert config.args["range"] == "today"


def test_fetcher_config_without_secrets() -> None:
    config = FetcherConfig(image="fetcher-weather:latest", args={"location": "nyc"})
    assert config.secrets is None


def test_multiple_fetchers(tmp_path: Path) -> None:
    task = {
        "name": "multi_fetch",
        "schedule": "0 7 * * *",
        "fetch": {
            "weather": {"image": "fetcher-weather:latest", "args": {"location": "sf"}},
            "calendar": {
                "image": "fetcher-gcal:latest",
                "secrets": "secrets/gcal.env.enc",
                "args": {"range": "today"},
            },
        },
        "prompt": "Weather: {weather}\nCalendar: {calendar}",
        "output": {"type": "stdout", "to": ""},
    }
    path = tmp_path / "multi.yaml"
    path.write_text(yaml.dump(task))
    loaded = load_task(path)
    assert len(loaded.fetch) == 2
    assert "weather" in loaded.fetch
    assert "calendar" in loaded.fetch
