"""Integration smoke test — full pipeline from message to response."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creel.chat import ChatServer
from creel.models import AgentDefinition


class TestFullPipeline:
    """End-to-end: incoming message → ChatServer → agent loop → mock LLM → response."""

    @patch("creel.agent.call_llm")
    def test_stdin_to_response(
        self, mock_call_llm, minimal_agent_def: AgentDefinition, monkeypatch
    ):
        """A user message should flow through ChatServer and return a text response."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Mock LLM returns a simple text response (no tool calls)
        block = MagicMock()
        block.type = "text"
        block.text = "The weather is sunny today!"
        response = MagicMock()
        response.content = [block]
        response.stop_reason = "end_turn"
        mock_call_llm.return_value = response

        server = ChatServer(minimal_agent_def, use_containers=False)
        result = server.handle_message("test-user", "What's the weather?")

        assert "sunny" in result
        mock_call_llm.assert_called()
        # Verify the user message was passed through
        call_kwargs = mock_call_llm.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages") or call_kwargs[0][0]
        assert any(
            m.get("content") == "What's the weather?"
            for m in messages
            if isinstance(m, dict)
        )

    @patch("creel.agent.call_llm")
    def test_session_persists_across_messages(
        self, mock_call_llm, minimal_agent_def: AgentDefinition, monkeypatch
    ):
        """Multiple messages from the same sender should share a session."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        block = MagicMock()
        block.type = "text"
        block.text = "Response"
        response = MagicMock()
        response.content = [block]
        response.stop_reason = "end_turn"
        mock_call_llm.return_value = response

        server = ChatServer(minimal_agent_def, use_containers=False)
        server.handle_message("test-user", "First message")
        server.handle_message("test-user", "Second message")

        # Second call should have both messages in history
        second_call = mock_call_llm.call_args_list[1]
        messages = second_call.kwargs.get("messages") or second_call[1].get("messages") or second_call[0][0]
        user_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)]
        assert len(user_messages) >= 2

    @patch("creel.agent.call_llm")
    def test_clear_resets_session(
        self, mock_call_llm, minimal_agent_def: AgentDefinition, monkeypatch
    ):
        """The 'clear' command should reset the session."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        block = MagicMock()
        block.type = "text"
        block.text = "Hi"
        response = MagicMock()
        response.content = [block]
        response.stop_reason = "end_turn"
        mock_call_llm.return_value = response

        server = ChatServer(minimal_agent_def, use_containers=False)
        server.handle_message("test-user", "Hello")
        result = server.handle_message("test-user", "clear")
        assert "cleared" in result.lower()

    @patch("creel.agent.call_llm")
    def test_tool_call_round_trip(
        self, mock_call_llm, minimal_agent_def: AgentDefinition, monkeypatch
    ):
        """Agent loop should handle a tool call and return final text."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # First call: LLM wants to use a tool
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tool_123"
        tool_block.name = "test_tool"
        tool_block.input = {"query": "test"}
        first_response = MagicMock()
        first_response.content = [tool_block]
        first_response.stop_reason = "tool_use"

        # Second call: LLM gives final text
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Here is the result!"
        second_response = MagicMock()
        second_response.content = [text_block]
        second_response.stop_reason = "end_turn"

        mock_call_llm.side_effect = [first_response, second_response]

        # Add a tool to the agent definition
        from creel.models import ToolConfig
        minimal_agent_def.tools = {
            "test_tool": ToolConfig(
                executor="weather",
                description="Test tool",
            )
        }

        server = ChatServer(minimal_agent_def, use_containers=False)

        # Mock the tool execution
        with patch("creel.agent.execute_tool_call", return_value="Tool output"):
            result = server.handle_message("test-user", "Use the tool")

        assert "result" in result.lower() or "Here is" in result
        assert mock_call_llm.call_count == 2


class TestSlashCommands:
    """Tests for /status and /model slash commands."""

    def test_status_command(self, minimal_agent_def: AgentDefinition, monkeypatch):
        """/status should return server status info without calling the LLM."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        server = ChatServer(minimal_agent_def, use_containers=False)
        result = server.handle_message("test-user", "/status")

        assert "Status:" in result
        assert "Model:" in result
        assert "Session ID:" in result
        assert "Messages:" in result
        assert "Uptime:" in result
        assert "Guardian: disabled" in result

    def test_model_command(self, minimal_agent_def: AgentDefinition, monkeypatch):
        """/model should return model configuration without calling the LLM."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        server = ChatServer(minimal_agent_def, use_containers=False)
        result = server.handle_message("test-user", "/model")

        assert "Model:" in result
        assert "Name:" in result
        assert minimal_agent_def.llm.model in result
        assert "Max tokens:" in result
        assert "Max turns:" in result
