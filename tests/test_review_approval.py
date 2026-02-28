"""Tests for async approval queue and REVIEW verdict flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from guardian.types import ActionDecision, ActionVerdict
from taskrunner.approvals import ApprovalQueue

# ── ApprovalQueue tests ─────────────────────────────────────────────


class TestApprovalQueue:
    def test_add_and_get_pending(self, tmp_path):
        q = ApprovalQueue(approvals_dir=str(tmp_path / "approvals"))
        action = q.add("sender1", "send_email", {"to": "a@b.com"}, "flagged")
        assert action.status == "pending"
        assert action.tool_name == "send_email"

        found = q.get_pending("sender1")
        assert found is not None
        assert found.id == action.id

    def test_get_pending_returns_none_when_empty(self, tmp_path):
        q = ApprovalQueue(approvals_dir=str(tmp_path / "approvals"))
        assert q.get_pending("nobody") is None

    def test_resolve_approved(self, tmp_path):
        q = ApprovalQueue(approvals_dir=str(tmp_path / "approvals"))
        action = q.add("sender1", "tool", {}, "reason")
        q.resolve(action.id, approved=True)
        resolved = q.get_resolved(action.id)
        assert resolved is not None
        assert resolved.status == "approved"
        # No longer pending
        assert q.get_pending("sender1") is None

    def test_resolve_denied(self, tmp_path):
        q = ApprovalQueue(approvals_dir=str(tmp_path / "approvals"))
        action = q.add("sender1", "tool", {}, "reason")
        q.resolve(action.id, approved=False)
        resolved = q.get_resolved(action.id)
        assert resolved.status == "denied"

    def test_persistence(self, tmp_path):
        d = str(tmp_path / "approvals")
        q1 = ApprovalQueue(approvals_dir=d)
        action = q1.add("sender1", "tool", {"k": "v"}, "reason")

        # Load fresh from disk
        q2 = ApprovalQueue(approvals_dir=d)
        found = q2.get_pending("sender1")
        assert found is not None
        assert found.id == action.id
        assert found.tool_input == {"k": "v"}

    def test_cleanup_removes_old(self, tmp_path):
        q = ApprovalQueue(approvals_dir=str(tmp_path / "approvals"))
        action = q.add("sender1", "tool", {}, "reason")
        # Manually backdate
        q._actions[action.id].created_at = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        q._save()

        removed = q.cleanup(max_age_hours=24)
        assert removed == 1
        assert q.get_pending("sender1") is None

    def test_get_pending_returns_most_recent(self, tmp_path):
        q = ApprovalQueue(approvals_dir=str(tmp_path / "approvals"))
        q.add("sender1", "tool1", {}, "reason")
        a2 = q.add("sender1", "tool2", {}, "reason")
        found = q.get_pending("sender1")
        assert found.id == a2.id


# ── Agent loop REVIEW → approval_required tests ─────────────────────


class _FakeCoherence:
    coherent = True
    reasoning = ""


class FakeGuardian:
    def __init__(self, verdict: ActionVerdict, reason: str = "test reason"):
        self._verdict = verdict
        self._reason = reason
        self._audit = None

    def validate_action(self, tool_name, tool_input):
        return ActionDecision(verdict=self._verdict, tool_name=tool_name, reason=self._reason)

    def log_action_outcome(self, tool_name, stage, outcome):
        pass

    def check_coherence(
        self, user_request, tool_name, tool_input, prior_tools=None, available_tools=None
    ):
        return _FakeCoherence()

    def check_drift(self, tool_name, output_length, success):
        return []

    def screen_tool_result(self, tool_name, result):
        return MagicMock(blocked=False)


class FakeToolUse:
    type = "tool_use"

    def __init__(self, name="send_email", input=None):
        self.id = "tool_123"
        self.name = name
        self.input = input or {"to": "a@b.com", "subject": "hi", "body": "hello"}


class FakeTextBlock:
    type = "text"

    def __init__(self, text="Done."):
        self.text = text


class FakeResponse:
    def __init__(self, tool_name="send_email", tool_input=None):
        self.content = [FakeToolUse(tool_name, tool_input)]


class FakeTextResponse:
    def __init__(self, text="Done."):
        self.content = [FakeTextBlock(text)]


@patch("taskrunner.agent.execute_tool_call", return_value="ok")
@patch("taskrunner.agent.call_llm")
def test_review_returns_approval_required(mock_llm, mock_exec):
    """REVIEW verdict → stop_reason='approval_required', tool NOT executed."""
    from taskrunner.agent import run_agent_loop
    from taskrunner.models import AgentConfig, LLMConfig

    mock_llm.return_value = FakeResponse()

    result = run_agent_loop(
        messages=[{"role": "user", "content": "send an email"}],
        llm_config=LLMConfig(model="test"),
        tools_config={"send_email": MagicMock()},
        agent_config=AgentConfig(max_turns=5),
        guardian=FakeGuardian(ActionVerdict.REVIEW),
    )

    assert result.stop_reason == "approval_required"
    assert result.pending_approval is not None
    assert result.pending_approval.tool_name == "send_email"
    mock_exec.assert_not_called()


@patch("taskrunner.agent.execute_tool_call", return_value="ok")
@patch("taskrunner.agent.call_llm")
def test_review_with_confirm_action_approves(mock_llm, mock_exec):
    """REVIEW verdict + confirm_action that returns True → tool executes."""
    from taskrunner.agent import run_agent_loop
    from taskrunner.models import AgentConfig, LLMConfig

    mock_llm.side_effect = [FakeResponse(), FakeTextResponse()]

    auto_confirm = MagicMock(return_value=True)
    result = run_agent_loop(
        messages=[{"role": "user", "content": "send an email"}],
        llm_config=LLMConfig(model="test"),
        tools_config={"send_email": MagicMock()},
        agent_config=AgentConfig(max_turns=5),
        guardian=FakeGuardian(ActionVerdict.REVIEW),
        confirm_action=auto_confirm,
    )

    assert result.stop_reason == "end_turn"
    auto_confirm.assert_called_once()
    mock_exec.assert_called_once()


@patch("taskrunner.agent.execute_tool_call", return_value="ok")
@patch("taskrunner.agent.call_llm")
def test_deny_still_denies(mock_llm, mock_exec):
    """DENY verdict still denies inline."""
    from taskrunner.agent import run_agent_loop
    from taskrunner.models import AgentConfig, LLMConfig

    mock_llm.side_effect = [FakeResponse(), FakeTextResponse()]

    result = run_agent_loop(
        messages=[{"role": "user", "content": "send an email"}],
        llm_config=LLMConfig(model="test"),
        tools_config={"send_email": MagicMock()},
        agent_config=AgentConfig(max_turns=5),
        guardian=FakeGuardian(ActionVerdict.DENY),
    )

    mock_exec.assert_not_called()
    assert result.stop_reason == "end_turn"
    assert any("denied" in h["output"].lower() for h in result.tool_history)


# ── ChatServer approval flow integration tests ──────────────────────


def _make_chat_server(tmp_path, guardian=None, imessage_channel=None):
    """Create a ChatServer with a real AgentDefinition so __init__ stays in sync."""
    from taskrunner.chat import ChatServer
    from taskrunner.models import (
        AgentDefinition,
        SessionConfig,
        ToolConfig,
        WorkspaceConfig,
    )

    agent_def = AgentDefinition(
        system_prompt="You are a test agent.",
        tools={
            "send_email": ToolConfig(executor="mock", description="Send an email"),
        },
        session=SessionConfig(
            sessions_dir=str(tmp_path / "sessions"),
            max_history=10,
            summarize_on_trim=False,
        ),
        workspace=WorkspaceConfig(
            # Non-existent dir so MemoryManager is not created
            path=str(tmp_path / "workspace-does-not-exist"),
        ),
        guardian=guardian,
    )

    server = ChatServer(
        agent_def,
        use_containers=False,
        reply_channel=imessage_channel,
    )
    return server


@patch("taskrunner.chat.run_agent_loop")
def test_chat_approval_required_queues_action(mock_run, tmp_path):
    """When agent returns approval_required, ChatServer queues it."""
    from taskrunner.agent import AgentResult, PendingApproval

    mock_run.return_value = AgentResult(
        text="Needs approval",
        turns_used=1,
        tool_calls_made=0,
        stop_reason="approval_required",
        pending_approval=PendingApproval("send_email", {"to": "x@y.com"}, "flagged"),
    )

    server = _make_chat_server(tmp_path)
    response = server.handle_message("sender1", "send email to x@y.com")

    assert "approval" in response.lower() or "⏳" in response
    pending = server._approval_queue.get_pending("sender1")
    assert pending is not None
    assert pending.tool_name == "send_email"


@patch("taskrunner.chat.execute_tool_call", return_value="Email sent!")
def test_chat_y_approves_and_executes(mock_exec, tmp_path):
    """Replying 'Y' resolves pending action and executes the tool."""
    server = _make_chat_server(tmp_path)
    server._approval_queue.add("sender1", "send_email", {"to": "x@y.com"}, "flagged")

    response = server.handle_message("sender1", "Y")

    assert "✅" in response
    assert "send_email" in response
    mock_exec.assert_called_once_with(
        tool_name="send_email",
        tool_input={"to": "x@y.com"},
        tools_config=server._agent_def.tools,
        use_containers=False,
        memory_manager=server._memory,
        bridge_config=server._agent_def.bridge,
        session_state=server._session_states.get("sender1"),
    )


def test_chat_n_denies(tmp_path):
    """Replying 'N' denies the pending action."""
    server = _make_chat_server(tmp_path)
    action = server._approval_queue.add("sender1", "send_email", {"to": "x@y.com"}, "flagged")

    response = server.handle_message("sender1", "n")

    assert "❌" in response or "denied" in response.lower()
    resolved = server._approval_queue.get_resolved(action.id)
    assert resolved.status == "denied"


@patch("taskrunner.chat.run_agent_loop")
def test_chat_no_pending_passes_to_agent(mock_run, tmp_path):
    """'Y' with no pending action goes to normal agent flow."""
    from taskrunner.agent import AgentResult

    mock_run.return_value = AgentResult(
        text="I don't understand.",
        turns_used=1,
        tool_calls_made=0,
        stop_reason="end_turn",
    )

    server = _make_chat_server(tmp_path)
    server.handle_message("sender1", "Y")

    # Should have gone to agent loop, not approval handler
    mock_run.assert_called_once()


@patch("taskrunner.chat.run_agent_loop")
def test_chat_auto_approve_passes_confirm_action(mock_run, tmp_path):
    """auto_approve=True passes a confirm_action callback to agent loop."""
    from taskrunner.agent import AgentResult

    mock_run.return_value = AgentResult(
        text="Task created.",
        turns_used=1,
        tool_calls_made=1,
        stop_reason="end_turn",
    )

    server = _make_chat_server(tmp_path)
    response = server.handle_message("sender1", "add task", auto_approve=True)

    assert response == "Task created."
    # Verify confirm_action was passed (not None)
    call_kwargs = mock_run.call_args
    confirm_fn = call_kwargs.kwargs.get("confirm_action") or call_kwargs[1].get("confirm_action")
    assert confirm_fn is not None
    # The auto-confirm callback should always return True
    assert confirm_fn("some_tool", {}, "test reason") is True


@patch("taskrunner.chat.run_agent_loop")
def test_chat_no_auto_approve_has_no_confirm(mock_run, tmp_path):
    """Without auto_approve, confirm_action is None (no _confirm_fn set)."""
    from taskrunner.agent import AgentResult

    mock_run.return_value = AgentResult(
        text="Done.",
        turns_used=1,
        tool_calls_made=0,
        stop_reason="end_turn",
    )

    server = _make_chat_server(tmp_path)
    server.handle_message("sender1", "hello")

    call_kwargs = mock_run.call_args
    confirm_fn = call_kwargs.kwargs.get("confirm_action") or call_kwargs[1].get("confirm_action")
    assert confirm_fn is None


@patch("taskrunner.chat.run_agent_loop")
def test_chat_pending_approval_blocks_new_message(mock_run, tmp_path):
    """A new non-approval message should be blocked while approval is pending."""
    server = _make_chat_server(tmp_path)
    server._approval_queue.add("sender1", "send_email", {"to": "x@y.com"}, "flagged")

    response = server.handle_message("sender1", "send another email")

    assert "pending" in response.lower()
    assert "approve" in response.lower()
    mock_run.assert_not_called()


@patch("taskrunner.agent.call_llm")
def test_orphaned_tool_use_is_repaired_before_llm_call(mock_llm):
    """Agent loop should inject synthetic tool_result before the next user turn."""
    from taskrunner.agent import run_agent_loop
    from taskrunner.models import AgentConfig, LLMConfig

    mock_llm.return_value = FakeTextResponse("Recovered")
    messages = [
        {"role": "user", "content": "send email"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_orphan",
                    "name": "send_email",
                    "input": {"to": "x@y.com"},
                },
            ],
        },
        {"role": "user", "content": "actually nevermind"},
    ]

    result = run_agent_loop(
        messages=messages,
        llm_config=LLMConfig(model="test"),
        tools_config={"send_email": MagicMock()},
        agent_config=AgentConfig(max_turns=1),
        guardian=None,
    )

    assert result.stop_reason == "end_turn"
    repaired = [
        msg
        for msg in messages
        if msg.get("role") == "user"
        and isinstance(msg.get("content"), list)
        and any(
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") == "tool_orphan"
            for block in msg["content"]
        )
    ]
    assert repaired, "Expected synthetic tool_result for orphaned tool_use"


def test_partial_tool_results_repaired():
    """When some tool_results are present but others missing, only missing ones are injected."""
    from taskrunner.agent import _ensure_tool_call_integrity

    messages = [
        {"role": "user", "content": "do two things"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool_a", "name": "send_email", "input": {}},
                {"type": "tool_use", "id": "tool_b", "name": "check_weather", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_a",
                    "content": "sent",
                    "is_error": False,
                },
            ],
        },
        {"role": "user", "content": "what happened?"},
    ]

    count = _ensure_tool_call_integrity(messages)

    assert count == 1
    # The existing user tool_result message should now have 2 entries
    tool_result_msg = messages[2]
    assert len(tool_result_msg["content"]) == 2
    ids = {b["tool_use_id"] for b in tool_result_msg["content"]}
    assert ids == {"tool_a", "tool_b"}
    # The injected one should be an error
    injected = [b for b in tool_result_msg["content"] if b["tool_use_id"] == "tool_b"]
    assert injected[0]["is_error"] is True


def test_chat_approval_sends_imessage(tmp_path):
    """Approval request sends iMessage when channel is available."""
    mock_channel = MagicMock()
    agent_def = MagicMock()
    agent_def.channels.imessage.listen_to = "+1234567890"

    server = _make_chat_server(tmp_path, imessage_channel=mock_channel)
    server._agent_def.channels.imessage = agent_def.channels.imessage

    action = server._approval_queue.add("sender1", "send_email", {"to": "x@y.com"}, "flagged")
    server._send_approval_request("sender1", action)

    mock_channel.send.assert_called_once()
    sent_msg = mock_channel.send.call_args[0][1]
    assert "⚠️" in sent_msg
    assert "send_email" in sent_msg
