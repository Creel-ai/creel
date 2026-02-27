"""HARNESS-004: Tool execution integration tests.

Tests the agent's ability to call tools and process results end-to-end:
  - LLM requests a tool call -> tool executes -> result sent back to LLM -> final response
  - Non-zero exit code handling
  - Guardian blocks dangerous commands
  - Unknown tool name handled gracefully
"""

from __future__ import annotations

import json

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def send_message(
    client: httpx.Client,
    text: str,
    sender_id: str = "tool-test-sender",
    auto_approve: bool = False,
) -> httpx.Response:
    """POST /v1/messages and return the raw response."""
    body: dict = {"sender_id": sender_id, "text": text, "auto_approve": auto_approve}
    return client.post("/v1/messages", json=body)


def _get_tool_result_from_followup_call(history: dict) -> dict | None:
    """Get the tool_result block from the second (followup) mock LLM call.

    In a tool-call flow with a clean mock reset:
      Call 0: user text -> LLM returns tool_use
      Call 1: messages include tool_result -> LLM returns followup text

    The tool_result in Call 1's LAST user message is the one from the
    current test's tool execution (or Guardian block).
    """
    calls = history.get("calls", [])
    if len(calls) < 2:
        return None

    # Look at the second call (the followup after tool execution)
    followup_call = calls[1]
    messages = followup_call.get("body", {}).get("messages", [])

    # Find the last user message containing tool_result blocks
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return block
    return None


# ---------------------------------------------------------------------------
# Tests: Tool execution end-to-end
# ---------------------------------------------------------------------------


class TestToolExecution:
    """Message triggers tool call -> tool executes -> result sent back to LLM -> final response."""

    def test_exec_tool_call_returns_output(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """'run echo test' triggers exec tool, which runs 'echo test',
        and the final response includes the command output."""
        resp = send_message(daemon_client, "run echo test", sender_id="tool-exec-1")
        assert resp.status_code == 200
        data = resp.json()
        # The mock LLM followup is "The command output: {tool_result}"
        # tool_result is JSON with stdout containing "test"
        assert "test" in data["text"]

    def test_tool_output_in_llm_history(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """After tool execution, mock LLM history contains the tool_result
        with the actual command output."""
        send_message(daemon_client, "run echo test", sender_id="tool-exec-2")

        history = mock_client.get("/v1/mock/history").json()
        # Should have at least 2 calls:
        # 1) Initial: user text -> LLM returns tool_use
        # 2) Follow-up: messages include tool_result -> LLM returns text
        assert len(history["calls"]) >= 2

        # Find the tool_result in the followup call
        tool_result = _get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found in mock LLM followup call"

        # The tool_result content should be JSON from the exec executor
        # containing stdout with "test"
        result_content = tool_result.get("content", "")
        assert "test" in result_content, (
            f"Expected 'test' in tool_result content: {result_content[:300]}"
        )

    def test_tool_result_contains_exec_output_json(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """The tool_result is a JSON object from the exec executor with
        exit_code, stdout, stderr, and success fields."""
        send_message(daemon_client, "run echo test", sender_id="tool-exec-3")

        history = mock_client.get("/v1/mock/history").json()
        tool_result = _get_tool_result_from_followup_call(history)
        assert tool_result is not None

        result_content = tool_result.get("content", "")
        # Parse the JSON output from the exec executor
        parsed = json.loads(result_content)
        assert parsed["exit_code"] == 0
        assert parsed["success"] is True
        assert "test" in parsed["stdout"]


class TestNonZeroExitCode:
    """Tool with non-zero exit code reports error in tool result."""

    def test_failing_command_has_nonzero_exit_code(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """A command that exits non-zero (false) has exit_code != 0 in the tool result."""
        resp = send_message(daemon_client, "run failing command", sender_id="tool-fail-1")
        assert resp.status_code == 200

        # Verify via mock LLM history that the tool_result shows failure
        history = mock_client.get("/v1/mock/history").json()
        tool_result = _get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found after failing command"

        result_content = tool_result.get("content", "")
        parsed = json.loads(result_content)
        assert parsed["exit_code"] != 0, f"Expected non-zero exit code, got {parsed['exit_code']}"
        assert parsed["success"] is False

    def test_failing_command_response_is_200(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """A failing command still returns 200 — the daemon doesn't crash."""
        resp = send_message(daemon_client, "run failing command", sender_id="tool-fail-2")
        assert resp.status_code == 200
        data = resp.json()
        # Should have a text response (the followup from mock LLM)
        assert data["text"]


class TestGuardianBlocking:
    """Guardian-blocked commands are rejected — tool is NOT executed."""

    def test_rm_rf_blocked_by_guardian(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """'delete everything' triggers exec with 'rm -rf /' -> Guardian blocks it.
        The tool_result sent to the LLM indicates denial."""
        resp = send_message(daemon_client, "delete everything", sender_id="tool-guardian-1")
        assert resp.status_code == 200

        # The tool_result in mock LLM history should contain the denial message
        history = mock_client.get("/v1/mock/history").json()
        tool_result = _get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found after blocked command"

        result_content = str(tool_result.get("content", ""))
        assert "denied" in result_content.lower() or "security policy" in result_content.lower(), (
            f"Expected denial message in tool_result, got: {result_content[:300]}"
        )

    def test_rm_rf_tool_result_is_error(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """The tool_result for a blocked command has is_error=True."""
        send_message(daemon_client, "delete everything", sender_id="tool-guardian-2")

        history = mock_client.get("/v1/mock/history").json()
        tool_result = _get_tool_result_from_followup_call(history)
        assert tool_result is not None

        assert tool_result.get("is_error") is True, (
            "Expected is_error=True for Guardian-blocked tool_result"
        )

    def test_curl_pipe_bash_blocked(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """'pipe to bash' triggers exec with 'curl ... | bash' -> Guardian blocks it."""
        resp = send_message(daemon_client, "pipe to bash", sender_id="tool-guardian-3")
        assert resp.status_code == 200

        history = mock_client.get("/v1/mock/history").json()
        tool_result = _get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found after blocked curl|bash command"

        result_content = str(tool_result.get("content", ""))
        assert "denied" in result_content.lower() or "security policy" in result_content.lower(), (
            f"Expected denial message for curl|bash, got: {result_content[:300]}"
        )


class TestUnknownTool:
    """Unknown tool name in LLM response is handled gracefully."""

    def test_unknown_tool_no_crash(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """LLM requesting an unknown tool doesn't crash the daemon.
        auto_approve=True bypasses the REVIEW verdict for unknown tools."""
        resp = send_message(
            daemon_client,
            "call unknown tool",
            sender_id="tool-unknown-1",
            auto_approve=True,
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should return some response text, not an error
        assert data["text"]

    def test_unknown_tool_error_in_history(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """When an unknown tool is called, the tool_result contains an error."""
        send_message(
            daemon_client,
            "call unknown tool",
            sender_id="tool-unknown-2",
            auto_approve=True,
        )

        history = mock_client.get("/v1/mock/history").json()
        tool_result = _get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found for unknown tool call"

        result_content = str(tool_result.get("content", ""))
        # The error message from execute_tool_call is "Error: Unknown tool: nonexistent_tool"
        assert "unknown" in result_content.lower() or "error" in result_content.lower(), (
            f"Expected error about unknown tool, got: {result_content[:300]}"
        )
