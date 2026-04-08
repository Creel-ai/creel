"""Tests for task definition loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from creel.models import (
    AgentConfig,
    ExecutorConfig,
    SessionConfig,
    ToolConfig,
    ToolParameter,
    WorkspaceConfig,
    load_agent_config,
    load_task,
)


@pytest.fixture
def valid_task_yaml(tmp_path: Path) -> Path:
    """Create a valid task YAML file."""
    task = {
        "name": "test_task",
        "schedule": "0 7 * * *",
        "executors": {
            "weather": {
                "args": {"location": "denver"},
            }
        },
        "prompt": "Today is {date}. Weather: {weather}",
        "output": {"type": "stdout", "to": ""},
        "llm": {"model": "claude-sonnet-4-6", "max_tokens": 100},
    }
    path = tmp_path / "test_task.yaml"
    path.write_text(yaml.dump(task))
    return path


def test_load_valid_task(valid_task_yaml: Path) -> None:
    task = load_task(valid_task_yaml)
    assert task.name == "test_task"
    assert task.schedule == "0 7 * * *"
    assert "weather" in task.executors
    assert task.executors["weather"].name == "weather"
    assert task.executors["weather"].image == "executor-weather:latest"
    assert task.output.type == "stdout"
    assert task.llm.model == "claude-sonnet-4-6"


def test_load_task_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_task("/nonexistent/path.yaml")


def test_invalid_cron_schedule(tmp_path: Path) -> None:
    task = {
        "name": "bad_cron",
        "schedule": "not a cron",
        "executors": {},
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
        "executors": {},
        "prompt": "test",
        "output": {"type": "carrier_pigeon", "to": "someone"},
    }
    path = tmp_path / "bad_output.yaml"
    path.write_text(yaml.dump(task))
    with pytest.raises(Exception):
        load_task(path)


def test_default_llm_config(tmp_path: Path) -> None:
    task = {
        "name": "defaults",
        "schedule": "0 7 * * *",
        "executors": {},
        "prompt": "test",
        "output": {"type": "stdout", "to": ""},
    }
    path = tmp_path / "defaults.yaml"
    path.write_text(yaml.dump(task))
    loaded = load_task(path)
    assert loaded.llm.model == "claude-sonnet-4-6"
    assert loaded.llm.max_tokens == 300


def test_executor_config_with_secrets() -> None:
    config = ExecutorConfig(
        name="gcal",
        secrets="secrets/gcal.env.enc",
        args={"range": "today"},
    )
    assert config.secrets == "secrets/gcal.env.enc"
    assert config.args["range"] == "today"
    assert config.image == "executor-gcal:latest"


def test_executor_config_without_secrets() -> None:
    config = ExecutorConfig(name="weather", args={"location": "nyc"})
    assert config.secrets is None
    assert config.image == "executor-weather:latest"


def test_multiple_executors(tmp_path: Path) -> None:
    task = {
        "name": "multi_fetch",
        "schedule": "0 7 * * *",
        "executors": {
            "weather": {"args": {"location": "sf"}},
            "calendar": {
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
    assert len(loaded.executors) == 2
    assert "weather" in loaded.executors
    assert "calendar" in loaded.executors


# --- Tool / Agent model tests ---


def test_tool_parameter_defaults() -> None:
    param = ToolParameter()
    assert param.type == "string"
    assert param.description == ""
    assert param.required is False


def test_tool_config() -> None:
    cfg = ToolConfig(
        executor="gmail_modify",
        secrets="secrets/gmail_modify.env.enc",
        description="Trash an email",
        parameters={
            "message_id": ToolParameter(type="string", required=True),
        },
        fixed_args={"action": "trash"},
    )
    assert cfg.executor == "gmail_modify"
    assert "message_id" in cfg.parameters
    assert cfg.fixed_args["action"] == "trash"


def test_agent_config_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.max_turns == 10


def test_agent_config_bounds() -> None:
    with pytest.raises(Exception):
        AgentConfig(max_turns=0)
    with pytest.raises(Exception):
        AgentConfig(max_turns=51)


def test_session_config_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CREEL_HOME", str(tmp_path))
    cfg = SessionConfig()
    assert cfg.sessions_dir == str(tmp_path / "sessions")


def test_session_config_absolute_path_unchanged() -> None:
    cfg = SessionConfig(sessions_dir="/data/sessions")
    assert cfg.sessions_dir == "/data/sessions"


class TestWorkspaceConfigPath:
    """Tests for WorkspaceConfig.path resolution (#279)."""

    def test_relative_resolved_to_creel_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        cfg = WorkspaceConfig(path="workspace")
        assert cfg.path == str(tmp_path / "workspace")

    def test_default_resolved(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        cfg = WorkspaceConfig()
        assert cfg.path == str(tmp_path / "workspace")

    def test_absolute_path_unchanged(self) -> None:
        cfg = WorkspaceConfig(path="/data/my-workspace")
        assert cfg.path == "/data/my-workspace"

    def test_tilde_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/Users/testuser")
        cfg = WorkspaceConfig(path="~/my-workspace")
        assert cfg.path == "/Users/testuser/my-workspace"


def test_task_definition_mode_default(tmp_path: Path) -> None:
    """Default mode should be 'simple'."""
    task = {
        "name": "simple_task",
        "schedule": "0 7 * * *",
        "executors": {},
        "prompt": "test",
        "output": {"type": "stdout", "to": ""},
    }
    path = tmp_path / "simple.yaml"
    path.write_text(yaml.dump(task))
    loaded = load_task(path)
    assert loaded.mode == "simple"


def test_task_definition_agent_mode(tmp_path: Path) -> None:
    """Agent mode tasks should parse tools and agent config."""
    task = {
        "name": "agent_task",
        "schedule": "0 8 * * *",
        "mode": "agent",
        "executors": {},
        "prompt": "Triage emails",
        "output": {"type": "stdout", "to": ""},
        "tools": {
            "trash_email": {
                "executor": "gmail_modify",
                "description": "Trash email",
                "parameters": {
                    "message_id": {"type": "string", "required": True},
                },
                "fixed_args": {"action": "trash"},
            },
        },
        "agent": {"max_turns": 5},
    }
    path = tmp_path / "agent_task.yaml"
    path.write_text(yaml.dump(task))
    loaded = load_task(path)
    assert loaded.mode == "agent"
    assert "trash_email" in loaded.tools
    assert loaded.tools["trash_email"].fixed_args["action"] == "trash"
    assert loaded.agent.max_turns == 5


def test_invalid_mode(tmp_path: Path) -> None:
    task = {
        "name": "bad_mode",
        "schedule": "0 7 * * *",
        "executors": {},
        "prompt": "test",
        "output": {"type": "stdout", "to": ""},
        "mode": "invalid",
    }
    path = tmp_path / "bad_mode.yaml"
    path.write_text(yaml.dump(task))
    with pytest.raises(Exception):
        load_task(path)


def test_load_agent_config(tmp_path: Path) -> None:
    config = {
        "system_prompt": "You are helpful. Today is {date}.",
        "skills": {
            "weather": {
                "enabled": True,
            },
        },
        "llm": {"model": "claude-sonnet-4-6", "max_tokens": 1024},
        "agent": {"max_turns": 15},
        "session": {"sessions_dir": "sessions"},
    }
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.dump(config))

    agent_def = load_agent_config(path)
    assert agent_def.system_prompt.startswith("You are helpful")
    assert "weather" in agent_def.skills
    assert agent_def.agent.max_turns == 15
    assert agent_def.llm.max_tokens == 1024


def test_load_agent_config_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_agent_config("/nonexistent/agent.yaml")
