"""Tests for REVIEW verdict fail-closed behavior and iMessage approval flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from guardian.types import ActionDecision, ActionVerdict


class FakeGuardian:
    """Minimal guardian that returns a configurable verdict."""

    def __init__(self, verdict: ActionVerdict, reason: str = "test reason"):
        self._verdict = verdict
        self._reason = reason

    def validate_action(self, tool_name: str, tool_input: dict) -> ActionDecision:
        return ActionDecision(
            verdict=self._verdict,
            tool_name=tool_name,
            reason=self._reason,
        )


class FakeResponse:
    """Minimal LLM response with one tool_use block then a text block."""

    def __init__(self, tool_name: str = "send_email", tool_input: dict | None = None):
        self._tool_name = tool_name
        self._tool_input = tool_input or {"to": "a@b.com", "subject": "hi", "body": "hello"}
        self.content = [
            FakeToolUse(self._tool_name, self._tool_input),
        ]


class FakeTextResponse:
    def __init__(self, text: str = "Done."):
        self.content = [FakeTextBlock(text)]


class FakeToolUse:
    type = "tool_use"

    def __init__(self, name: str, input: dict):
        self.id = "tool_123"
        self.name = name
        self.input = input


class FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


# ── Core fail-closed tests ──────────────────────────────────────────


@patch("taskrunner.agent.execute_tool_call", return_value="ok")
@patch("taskrunner.agent.call_llm")
def test_review_with_no_confirm_fn_denies(mock_llm, mock_exec):
    """REVIEW verdict + no confirm_fn → action DENIED (fail-closed)."""
    from taskrunner.agent import run_agent_loop
    from taskrunner.models import AgentConfig, LLMConfig

    # First call returns tool_use, second returns text
    mock_llm.side_effect = [FakeResponse(), FakeTextResponse("Done.")]

    result = run_agent_loop(
        messages=[{"role": "user", "content": "send an email"}],
        llm_config=LLMConfig(model="test"),
        tools_config={"send_email": MagicMock()},
        agent_config=AgentConfig(max_turns=5),
        guardian=FakeGuardian(ActionVerdict.REVIEW),
        confirm_action=None,  # No confirm handler
    )

    # Tool should NOT have been executed
    mock_exec.assert_not_called()
    # Should have denied
    assert any("denied" in h["output"].lower() for h in result.tool_history)


@patch("taskrunner.agent.execute_tool_call", return_value="ok")
@patch("taskrunner.agent.call_llm")
def test_review_with_confirm_fn_true_proceeds(mock_llm, mock_exec):
    """REVIEW verdict + confirm_fn returns True → action proceeds."""
    from taskrunner.agent import run_agent_loop
    from taskrunner.models import AgentConfig, LLMConfig

    mock_llm.side_effect = [FakeResponse(), FakeTextResponse("Done.")]

    result = run_agent_loop(
        messages=[{"role": "user", "content": "send an email"}],
        llm_config=LLMConfig(model="test"),
        tools_config={"send_email": MagicMock()},
        agent_config=AgentConfig(max_turns=5),
        guardian=FakeGuardian(ActionVerdict.REVIEW),
        confirm_action=lambda *a: True,
    )

    mock_exec.assert_called_once()


@patch("taskrunner.agent.execute_tool_call", return_value="ok")
@patch("taskrunner.agent.call_llm")
def test_review_with_confirm_fn_false_denies(mock_llm, mock_exec):
    """REVIEW verdict + confirm_fn returns False → action DENIED."""
    from taskrunner.agent import run_agent_loop
    from taskrunner.models import AgentConfig, LLMConfig

    mock_llm.side_effect = [FakeResponse(), FakeTextResponse("Done.")]

    result = run_agent_loop(
        messages=[{"role": "user", "content": "send an email"}],
        llm_config=LLMConfig(model="test"),
        tools_config={"send_email": MagicMock()},
        agent_config=AgentConfig(max_turns=5),
        guardian=FakeGuardian(ActionVerdict.REVIEW),
        confirm_action=lambda *a: False,
    )

    mock_exec.assert_not_called()
    assert any("denied" in h["output"].lower() for h in result.tool_history)


# ── Approval message formatting tests ───────────────────────────────


def test_imessage_confirm_formats_message():
    """Test that the approval message is well-formatted."""
    from taskrunner.chat import ChatServer
    from taskrunner.models import AgentDefinition

    # Minimal mock agent def
    agent_def = MagicMock()
    agent_def.channels.imessage.listen_to = "+1234567890"
    agent_def.guardian.review.timeout_seconds = 30
    agent_def.workspace.path = "/tmp/test"
    agent_def.workspace.timezone = "UTC"
    agent_def.workspace.memory_days = 1
    agent_def.workspace.memory_max_chars = 100
    agent_def.workspace.max_chars_per_file = 1000
    agent_def.session.sessions_dir = "/tmp/sessions"
    agent_def.session.max_history = 10
    agent_def.guardian.enabled = False  # skip guardian init
    agent_def.system_prompt = "test"
    agent_def.system_prompt_file = None

    mock_channel = MagicMock()
    mock_channel.send = MagicMock()
    mock_channel.wait_for_reply = MagicMock(return_value="Y")

    server = ChatServer.__new__(ChatServer)
    server._imessage_channel = mock_channel
    server._agent_def = agent_def

    result = server._imessage_confirm_action(
        "send_email",
        {"to": "ross@example.com", "subject": "Test"},
        "Flagged for review",
    )

    assert result is True
    mock_channel.send.assert_called_once()
    sent_msg = mock_channel.send.call_args[0][1]
    assert "send_email" in sent_msg
    assert "ross@example.com" in sent_msg
    assert "⚠️" in sent_msg


def test_imessage_confirm_timeout_denies():
    """Test that timeout results in denial."""
    from taskrunner.chat import ChatServer

    agent_def = MagicMock()
    agent_def.channels.imessage.listen_to = "+1234567890"
    agent_def.guardian.review.timeout_seconds = 5

    mock_channel = MagicMock()
    mock_channel.send = MagicMock()
    mock_channel.wait_for_reply = MagicMock(return_value=None)  # timeout

    server = ChatServer.__new__(ChatServer)
    server._imessage_channel = mock_channel
    server._agent_def = agent_def

    result = server._imessage_confirm_action("trash_email", {"message_id": "123"}, "review")
    assert result is False


def test_imessage_confirm_no_denies():
    """Test that 'N' reply results in denial."""
    from taskrunner.chat import ChatServer

    agent_def = MagicMock()
    agent_def.channels.imessage.listen_to = "+1234567890"
    agent_def.guardian.review.timeout_seconds = 60

    mock_channel = MagicMock()
    mock_channel.send = MagicMock()
    mock_channel.wait_for_reply = MagicMock(return_value="N")

    server = ChatServer.__new__(ChatServer)
    server._imessage_channel = mock_channel
    server._agent_def = agent_def

    result = server._imessage_confirm_action("send_email", {"to": "x@y.com"}, "review")
    assert result is False
