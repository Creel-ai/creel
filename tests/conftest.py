"""Shared test fixtures for the Creel test suite."""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import _pytest.pathlib
import _pytest.tmpdir
import pyrage
import pytest
import yaml

from creel.models import (
    AgentConfig,
    AgentDefinition,
    ChannelsConfig,
    LLMConfig,
    SessionConfig,
    WorkspaceConfig,
)

# ---------------------------------------------------------------------------
# Monkey-patch pytest's pathlib helpers to tolerate missing basetemp dirs.
# pytest 8.x's os.scandir(root) raises FileNotFoundError when /tmp cleanup
# or parallel runs delete the basetemp root between creation and scan.
# make_numbered_dir also fails because new_path.mkdir() lacks parents=True.
# Both are fixed upstream in pytest 9.x, but pytest-asyncio 0.24.x pins 8.x.
# ---------------------------------------------------------------------------
_orig_find_prefixed = _pytest.pathlib.find_prefixed
_orig_make_numbered_dir = _pytest.pathlib.make_numbered_dir


def _safe_find_prefixed(root: Path, prefix: str) -> Iterator[os.DirEntry[str]]:
    try:
        yield from _orig_find_prefixed(root, prefix)
    except FileNotFoundError:
        return


def _safe_make_numbered_dir(root: Path, prefix: str, mode: int = 0o700) -> Path:
    root.mkdir(mode=mode, parents=True, exist_ok=True)
    return _orig_make_numbered_dir(root, prefix, mode)


_pytest.pathlib.find_prefixed = _safe_find_prefixed
_pytest.pathlib.make_numbered_dir = _safe_make_numbered_dir
# Also patch the already-imported references in _pytest.tmpdir
_pytest.tmpdir.make_numbered_dir = _safe_make_numbered_dir


@pytest.fixture
def tmp_sessions_dir(tmp_path: Path) -> str:
    """Return a temporary sessions directory path."""
    d = tmp_path / "sessions"
    d.mkdir()
    return str(d)


@pytest.fixture
def minimal_agent_def(tmp_sessions_dir: str) -> AgentDefinition:
    """Create a minimal AgentDefinition for testing."""
    return AgentDefinition(
        system_prompt="You are a helpful assistant.",
        llm=LLMConfig(model="claude-sonnet-4-6", max_tokens=100),
        agent=AgentConfig(max_turns=5),
        session=SessionConfig(sessions_dir=tmp_sessions_dir),
        workspace=WorkspaceConfig(path="/tmp/nonexistent-workspace"),
        channels=ChannelsConfig(),
    )


@pytest.fixture
def mock_llm_response() -> MagicMock:
    """Create a mock Anthropic Message with a single text block."""
    block = MagicMock()
    block.type = "text"
    block.text = "Hello from the mock LLM!"
    msg = MagicMock()
    msg.content = [block]
    msg.stop_reason = "end_turn"
    return msg


@pytest.fixture
def cli_args(tmp_path: Path):
    """Factory fixture returning argparse.Namespace with common CLI attributes."""

    def _make(**overrides) -> argparse.Namespace:
        defaults = dict(
            tasks_dir=tmp_path / "tasks",
            agent_config=tmp_path / "agent.yaml",
            containers=False,
            no_judge=False,
            verbose=False,
            json_logs=False,
            channel_type="imessage",
            no_scheduler=False,
            socket_path=tmp_path / "daemon.sock",
            pid_file=tmp_path / "daemon.pid",
            log_file=tmp_path / "daemon.log",
            wait_seconds=1.0,
            timeout=1.0,
            label="com.creel.daemon.test",
            plist_path=tmp_path / "LaunchAgents" / "com.creel.daemon.test.plist",
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    return _make


@pytest.fixture
def age_keypair(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a fresh age keypair for encrypt/decrypt round-trip tests."""
    identity = pyrage.x25519.Identity.generate()
    recipient = identity.to_public()

    key_file = tmp_path / "key.txt"
    key_file.write_text(f"# created: test\n# public key: {str(recipient)}\n{str(identity)}\n")

    pub_file = tmp_path / "key.pub"
    pub_file.write_text(str(recipient) + "\n")

    return key_file, pub_file


@pytest.fixture
def sample_task_yaml(tmp_path: Path):
    """Factory fixture that writes a minimal valid task YAML and returns its path."""

    def _make(name: str = "test_task", **overrides) -> Path:
        task = {
            "name": name,
            "schedule": "0 7 * * *",
            "executors": {
                "weather": {"args": {"location": "denver"}},
            },
            "prompt": "Date: {date}\nWeather: {weather}",
            "output": {"type": "stdout", "to": ""},
            "llm": {"model": "claude-sonnet-4-6", "max_tokens": 100},
        }
        task.update(overrides)
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir(exist_ok=True)
        path = tasks_dir / f"{name}.yaml"
        path.write_text(yaml.dump(task))
        return path

    return _make
