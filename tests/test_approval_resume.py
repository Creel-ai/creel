"""Tests for context reminder injection after Guardian approval flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from creel.agent import AgentResult
from creel.chat import ChatServer
from creel.models import (
    AgentConfig,
    AgentDefinition,
    ChannelsConfig,
    LLMConfig,
    SessionConfig,
    WorkspaceConfig,
)


def _make_agent_def(tmp_path: Path, **overrides) -> AgentDefinition:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    defaults = dict(
        system_prompt="You are a test assistant.",
        llm=LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100),
        agent=AgentConfig(max_turns=3),
        session=SessionConfig(
            sessions_dir=str(sessions_dir),
            max_history=50,
            summarize_on_trim=False,
        ),
        workspace=WorkspaceConfig(path=str(tmp_path / "nonexistent-workspace")),
        channels=ChannelsConfig(),
    )
    defaults.update(overrides)
    return AgentDefinition(**defaults)


# ---------------------------------------------------------------------------
# _extract_last_assistant_text
# ---------------------------------------------------------------------------


class TestExtractLastAssistantText:
    def test_string_content(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "I will send an email now."},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        ]
        result = ChatServer._extract_last_assistant_text(messages)
        assert result == "I will send an email now."

    def test_block_content(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check the weather and send an email."},
                    {"type": "tool_use", "id": "t1", "name": "weather", "input": {}},
                ],
            },
        ]
        result = ChatServer._extract_last_assistant_text(messages)
        assert result == "Let me check the weather and send an email."

    def test_no_assistant_text(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "weather", "input": {}},
                ],
            },
        ]
        result = ChatServer._extract_last_assistant_text(messages)
        assert result is None

    def test_empty_messages(self) -> None:
        assert ChatServer._extract_last_assistant_text([]) is None

    def test_skips_empty_string(self) -> None:
        messages = [
            {"role": "assistant", "content": "First plan."},
            {"role": "assistant", "content": "   "},
        ]
        result = ChatServer._extract_last_assistant_text(messages)
        assert result == "First plan."


# ---------------------------------------------------------------------------
# _inject_context_reminder
# ---------------------------------------------------------------------------


class TestInjectContextReminder:
    def test_appends_to_user_message_with_blocks(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "result"},
                ],
            },
        ]
        ChatServer._inject_context_reminder(messages, "reminder text")
        assert len(messages) == 2  # no new message added
        blocks = messages[-1]["content"]
        assert blocks[-1] == {"type": "text", "text": "reminder text"}

    def test_converts_string_user_content(self) -> None:
        messages = [{"role": "user", "content": "original text"}]
        ChatServer._inject_context_reminder(messages, "reminder")
        content = messages[-1]["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0] == {"type": "text", "text": "original text"}
        assert content[1] == {"type": "text", "text": "reminder"}

    def test_adds_new_message_when_last_is_assistant(self) -> None:
        messages = [{"role": "assistant", "content": "hello"}]
        ChatServer._inject_context_reminder(messages, "reminder")
        assert len(messages) == 2
        assert messages[-1] == {"role": "user", "content": "reminder"}

    def test_adds_new_message_when_empty(self) -> None:
        messages: list[dict] = []
        ChatServer._inject_context_reminder(messages, "reminder")
        assert len(messages) == 1
        assert messages[-1] == {"role": "user", "content": "reminder"}


# ---------------------------------------------------------------------------
# Integration: approval resume injects context reminder
# ---------------------------------------------------------------------------


class TestApprovalResumeContext:
    def test_reminder_injected_after_approval(self, tmp_path) -> None:
        """After approval + resume, a context reminder message is injected."""
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        # Queue a pending approval
        server._approval_queue.add(
            sender_id="user1",
            tool_name="gmail_send",
            tool_input={"to": "a@b.com", "body": "hi"},
            reason="review needed",
            tool_use_id="tool_123",
        )

        # Pre-populate session with assistant plan text + tool_result placeholder
        session = server._session_mgr.get_or_create("user1")
        session.messages.extend(
            [
                {"role": "user", "content": "Send an email to a@b.com"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll send that email for you now."},
                        {
                            "type": "tool_use",
                            "id": "tool_123",
                            "name": "gmail_send",
                            "input": {"to": "a@b.com", "body": "hi"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_123",
                            "content": "Awaiting approval",
                            "is_error": True,
                        },
                    ],
                },
            ]
        )
        server._session_mgr.save_session(session)

        resume_result = AgentResult(
            text="Email sent successfully.",
            turns_used=1,
            tool_calls_made=0,
            stop_reason="end_turn",
        )

        captured_messages = {}

        def capture_agent_loop(messages, *args, **kwargs):
            # Capture the messages passed to the agent loop
            captured_messages["messages"] = [m.copy() for m in messages]
            return resume_result

        with (
            patch("creel.chat.execute_tool_call", return_value="sent ok"),
            patch("creel.chat.run_agent_loop", side_effect=capture_agent_loop),
        ):
            result = server.handle_message("user1", "y")

        assert result == "Email sent successfully."

        # Verify the last user message now contains the context reminder
        last_user = captured_messages["messages"][-1]
        assert last_user["role"] == "user"
        content = last_user["content"]
        assert isinstance(content, list)
        # Should have tool_result + reminder text blocks
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert len(text_blocks) >= 1
        reminder_text = text_blocks[-1]["text"]
        assert "[System: Resuming after tool approval]" in reminder_text
        assert "gmail_send" in reminder_text
        assert "succeeded" in reminder_text

    def test_reminder_includes_last_assistant_text(self, tmp_path) -> None:
        """The reminder includes the agent's last text before interruption."""
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        server._approval_queue.add(
            sender_id="user1",
            tool_name="weather",
            tool_input={"location": "Denver"},
            reason="review",
            tool_use_id="tool_456",
        )

        session = server._session_mgr.get_or_create("user1")
        plan_text = "Step 1: Check weather. Step 2: Send summary email."
        session.messages.extend(
            [
                {"role": "user", "content": "Get weather and email me"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": plan_text},
                        {
                            "type": "tool_use",
                            "id": "tool_456",
                            "name": "weather",
                            "input": {"location": "Denver"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_456",
                            "content": "Awaiting approval",
                            "is_error": True,
                        },
                    ],
                },
            ]
        )
        server._session_mgr.save_session(session)

        resume_result = AgentResult(
            text="Done.",
            turns_used=1,
            tool_calls_made=0,
            stop_reason="end_turn",
        )

        captured = {}

        def capture(messages, *args, **kwargs):
            captured["msgs"] = [m.copy() for m in messages]
            return resume_result

        with (
            patch("creel.chat.execute_tool_call", return_value="72F"),
            patch("creel.chat.run_agent_loop", side_effect=capture),
        ):
            server.handle_message("user1", "y")

        last_user = captured["msgs"][-1]
        text_blocks = [b for b in last_user["content"] if b.get("type") == "text"]
        reminder = text_blocks[-1]["text"]
        assert plan_text in reminder

    def test_failed_tool_includes_error_in_reminder(self, tmp_path) -> None:
        """When the tool fails, the reminder includes the error."""
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        server._approval_queue.add(
            sender_id="user1",
            tool_name="gmail_send",
            tool_input={},
            reason="review",
            tool_use_id="tool_789",
        )

        session = server._session_mgr.get_or_create("user1")
        session.messages.extend(
            [
                {"role": "user", "content": "send email"},
                {"role": "assistant", "content": "I'll send that now."},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tool_789", "name": "gmail_send", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_789",
                            "content": "Awaiting approval",
                            "is_error": True,
                        },
                    ],
                },
            ]
        )
        server._session_mgr.save_session(session)

        resume_result = AgentResult(
            text="Failed.",
            turns_used=1,
            tool_calls_made=0,
            stop_reason="end_turn",
        )

        captured = {}

        def capture(messages, *args, **kwargs):
            captured["msgs"] = [m.copy() for m in messages]
            return resume_result

        with (
            patch(
                "creel.chat.execute_tool_call",
                side_effect=RuntimeError("SMTP timeout"),
            ),
            patch("creel.chat.run_agent_loop", side_effect=capture),
        ):
            server.handle_message("user1", "y")

        last_user = captured["msgs"][-1]
        text_blocks = [b for b in last_user["content"] if b.get("type") == "text"]
        reminder = text_blocks[-1]["text"]
        assert "failed with:" in reminder
        assert "SMTP timeout" in reminder

    def test_long_error_truncated_in_reminder(self, tmp_path) -> None:
        """Long error messages are truncated to 200 chars in the reminder."""
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        server._approval_queue.add(
            sender_id="user1",
            tool_name="gmail_send",
            tool_input={},
            reason="review",
            tool_use_id="tool_trunc",
        )

        session = server._session_mgr.get_or_create("user1")
        session.messages.extend(
            [
                {"role": "user", "content": "send email"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_trunc",
                            "name": "gmail_send",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_trunc",
                            "content": "Awaiting approval",
                            "is_error": True,
                        },
                    ],
                },
            ]
        )
        server._session_mgr.save_session(session)

        resume_result = AgentResult(
            text="Failed.",
            turns_used=1,
            tool_calls_made=0,
            stop_reason="end_turn",
        )

        captured = {}

        def capture(messages, *args, **kwargs):
            captured["msgs"] = [m.copy() for m in messages]
            return resume_result

        long_error = "x" * 500
        with (
            patch(
                "creel.chat.execute_tool_call",
                side_effect=RuntimeError(long_error),
            ),
            patch("creel.chat.run_agent_loop", side_effect=capture),
        ):
            server.handle_message("user1", "y")

        last_user = captured["msgs"][-1]
        text_blocks = [b for b in last_user["content"] if b.get("type") == "text"]
        reminder = text_blocks[-1]["text"]
        assert "failed with:" in reminder
        # The error portion should be truncated — full "Error: " + 500 x's = 507 chars,
        # but only the first 200 chars of the stringified result should appear.
        # The reminder template adds a trailing "." after tool_status, so strip it.
        error_part = reminder.split("failed with: ")[1].split("\n")[0].rstrip(".")
        assert len(error_part) <= 200
        # Verify it was actually truncated (original is 507 chars)
        assert len(error_part) < 507

    def test_agent_loop_not_called_with_reminder_as_user_message(self, tmp_path) -> None:
        """_invoke_agent_loop should NOT receive the reminder as user_message.

        The reminder is injected into session messages directly. Passing it
        as user_message would pollute memory relevance search with reminder
        text instead of the user's original intent.
        """
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        server._approval_queue.add(
            sender_id="user1",
            tool_name="weather",
            tool_input={},
            reason="review",
            tool_use_id="tool_aaa",
        )

        session = server._session_mgr.get_or_create("user1")
        session.messages.extend(
            [
                {"role": "user", "content": "check weather"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tool_aaa", "name": "weather", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_aaa",
                            "content": "Awaiting",
                            "is_error": True,
                        },
                    ],
                },
            ]
        )
        server._session_mgr.save_session(session)

        resume_result = AgentResult(
            text="ok",
            turns_used=1,
            tool_calls_made=0,
            stop_reason="end_turn",
        )

        with (
            patch("creel.chat.execute_tool_call", return_value="72F"),
            patch.object(server, "_invoke_agent_loop", return_value=resume_result) as mock_invoke,
        ):
            server.handle_message("user1", "y")

        mock_invoke.assert_called_once()
        kwargs = mock_invoke.call_args
        user_msg = kwargs.kwargs.get("user_message")
        assert user_msg is None

    def test_no_prior_assistant_text_uses_simple_reminder(self, tmp_path) -> None:
        """When no assistant text is found, the reminder is simpler."""
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        server._approval_queue.add(
            sender_id="user1",
            tool_name="weather",
            tool_input={},
            reason="review",
            tool_use_id="tool_bbb",
        )

        session = server._session_mgr.get_or_create("user1")
        # Only tool_use blocks, no text in assistant messages
        session.messages.extend(
            [
                {"role": "user", "content": "check weather"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tool_bbb", "name": "weather", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_bbb",
                            "content": "Awaiting",
                            "is_error": True,
                        },
                    ],
                },
            ]
        )
        server._session_mgr.save_session(session)

        resume_result = AgentResult(
            text="ok",
            turns_used=1,
            tool_calls_made=0,
            stop_reason="end_turn",
        )

        captured = {}

        def capture(messages, *args, **kwargs):
            captured["msgs"] = [m.copy() for m in messages]
            return resume_result

        with (
            patch("creel.chat.execute_tool_call", return_value="72F"),
            patch("creel.chat.run_agent_loop", side_effect=capture),
        ):
            server.handle_message("user1", "y")

        last_user = captured["msgs"][-1]
        text_blocks = [b for b in last_user["content"] if b.get("type") == "text"]
        reminder = text_blocks[-1]["text"]
        assert "[System: Resuming after tool approval]" in reminder
        assert "previous response" not in reminder
        assert "Continue your original plan" in reminder

    def test_no_consecutive_user_messages(self, tmp_path) -> None:
        """The reminder must not create consecutive user messages."""
        agent_def = _make_agent_def(tmp_path)
        server = ChatServer(agent_def)

        server._approval_queue.add(
            sender_id="user1",
            tool_name="weather",
            tool_input={},
            reason="review",
            tool_use_id="tool_ccc",
        )

        session = server._session_mgr.get_or_create("user1")
        session.messages.extend(
            [
                {"role": "user", "content": "check weather"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking weather now."},
                        {"type": "tool_use", "id": "tool_ccc", "name": "weather", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_ccc",
                            "content": "Awaiting",
                            "is_error": True,
                        },
                    ],
                },
            ]
        )
        server._session_mgr.save_session(session)

        resume_result = AgentResult(
            text="ok",
            turns_used=1,
            tool_calls_made=0,
            stop_reason="end_turn",
        )

        captured = {}

        def capture(messages, *args, **kwargs):
            captured["msgs"] = list(messages)
            return resume_result

        with (
            patch("creel.chat.execute_tool_call", return_value="72F"),
            patch("creel.chat.run_agent_loop", side_effect=capture),
        ):
            server.handle_message("user1", "y")

        # Check no consecutive user messages
        msgs = captured["msgs"]
        for i in range(1, len(msgs)):
            if msgs[i].get("role") == "user" and msgs[i - 1].get("role") == "user":
                raise AssertionError(f"Consecutive user messages at index {i - 1} and {i}")
