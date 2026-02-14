"""Shared test fixtures for the Creel test suite."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from taskrunner.models import (
    AgentConfig,
    AgentDefinition,
    ChannelsConfig,
    LLMConfig,
    SessionConfig,
    WorkspaceConfig,
)


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
        llm=LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100),
        agent=AgentConfig(max_turns=5),
        session=SessionConfig(sessions_dir=tmp_sessions_dir, max_history=50),
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
