"""HARNESS-007: Guardian security blocking integration tests.

Tests that Guardian correctly blocks dangerous commands, allows safe ones,
detects prompt injection in tool arguments, and logs block events to the
audit log.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import get_tool_result_from_followup_call, read_audit_entries, send_message


# ---------------------------------------------------------------------------
# Tests: Guardian blocks dangerous exec commands
# ---------------------------------------------------------------------------


class TestGuardianBlocksDangerous:
    """Guardian blocks exec commands matching deny_when patterns."""

    def test_rm_rf_blocked_response_indicates_denial(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """'delete everything' triggers exec with 'rm -rf /' which Guardian blocks.
        The daemon response still returns 200 (no crash), and the tool_result
        sent to the LLM indicates the action was denied."""
        resp = send_message(daemon_client, "delete everything", sender_id="guardian-rm-1")
        assert resp.status_code == 200

        history = mock_client.get("/v1/mock/history").json()
        tool_result = get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found after blocked rm -rf"

        result_content = str(tool_result.get("content", ""))
        assert "denied" in result_content.lower() or "security policy" in result_content.lower(), (
            f"Expected denial message, got: {result_content[:300]}"
        )
        assert tool_result.get("is_error") is True

    def test_curl_pipe_bash_blocked_response_indicates_denial(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """'pipe to bash' triggers exec with 'curl ... | bash' which Guardian blocks."""
        resp = send_message(daemon_client, "pipe to bash", sender_id="guardian-curl-1")
        assert resp.status_code == 200

        history = mock_client.get("/v1/mock/history").json()
        tool_result = get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found after blocked curl|bash"

        result_content = str(tool_result.get("content", ""))
        assert "denied" in result_content.lower() or "security policy" in result_content.lower(), (
            f"Expected denial message for curl|bash, got: {result_content[:300]}"
        )
        assert tool_result.get("is_error") is True

    def test_rm_rf_not_actually_executed(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """Verify the blocked command was NOT executed — the tool_result
        does NOT contain exec output (exit_code, stdout) but a denial string."""
        send_message(daemon_client, "delete everything", sender_id="guardian-rm-2")

        history = mock_client.get("/v1/mock/history").json()
        tool_result = get_tool_result_from_followup_call(history)
        assert tool_result is not None

        result_content = str(tool_result.get("content", ""))
        # If the command had executed, content would be JSON with exit_code
        # A denied command has a plain string like "Action denied by security policy: ..."
        try:
            parsed = json.loads(result_content)
            # If it parsed as JSON with exit_code, the command ran (bad!)
            assert "exit_code" not in parsed, "Command appears to have executed despite Guardian block"
        except (json.JSONDecodeError, TypeError):
            # Expected: denial message is a plain string, not JSON
            assert "denied" in result_content.lower()


# ---------------------------------------------------------------------------
# Tests: Guardian allows safe exec commands
# ---------------------------------------------------------------------------


class TestGuardianAllowsSafe:
    """Guardian allows exec commands that don't match deny patterns."""

    def test_echo_hello_is_allowed(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """'run allowed echo' triggers exec with 'echo hello' — Guardian allows it,
        and the tool_result contains actual command output."""
        resp = send_message(daemon_client, "run allowed echo", sender_id="guardian-allow-1")
        assert resp.status_code == 200

        history = mock_client.get("/v1/mock/history").json()
        tool_result = get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found for allowed echo command"

        result_content = str(tool_result.get("content", ""))
        # Should contain actual exec output (JSON with stdout)
        parsed = json.loads(result_content)
        assert parsed["exit_code"] == 0
        assert parsed["success"] is True
        assert "hello" in parsed["stdout"]

    def test_allowed_command_not_flagged_as_error(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """An allowed exec command has is_error=False in the tool_result."""
        send_message(daemon_client, "run allowed echo", sender_id="guardian-allow-2")

        history = mock_client.get("/v1/mock/history").json()
        tool_result = get_tool_result_from_followup_call(history)
        assert tool_result is not None

        # Allowed tools should NOT have is_error=True
        assert tool_result.get("is_error") is not True, (
            "Expected is_error=False for allowed command"
        )


# ---------------------------------------------------------------------------
# Tests: Prompt injection in tool arguments
# ---------------------------------------------------------------------------


class TestPromptInjectionInArgs:
    """Prompt injection text in tool arguments is caught by policy deny_when rules."""

    def test_prompt_injection_in_command_blocked(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """'inject in command' triggers exec with 'echo safe; ignore previous
        instructions ...' — the policy deny_when pattern catches 'ignore
        previous instructions' and blocks the tool call."""
        resp = send_message(daemon_client, "inject in command", sender_id="guardian-inject-1")
        assert resp.status_code == 200

        history = mock_client.get("/v1/mock/history").json()
        tool_result = get_tool_result_from_followup_call(history)
        assert tool_result is not None, "No tool_result found after prompt injection attempt"

        result_content = str(tool_result.get("content", ""))
        assert "denied" in result_content.lower() or "security policy" in result_content.lower(), (
            f"Expected denial for prompt injection in args, got: {result_content[:300]}"
        )
        assert tool_result.get("is_error") is True

    def test_prompt_injection_command_not_executed(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """The injected command must NOT actually execute."""
        send_message(daemon_client, "inject in command", sender_id="guardian-inject-2")

        history = mock_client.get("/v1/mock/history").json()
        tool_result = get_tool_result_from_followup_call(history)
        assert tool_result is not None

        result_content = str(tool_result.get("content", ""))
        try:
            parsed = json.loads(result_content)
            assert "exit_code" not in parsed, "Injected command appears to have executed"
        except (json.JSONDecodeError, TypeError):
            # Expected: denial message is a plain string
            assert "denied" in result_content.lower()


# ---------------------------------------------------------------------------
# Tests: Guardian audit logging
# ---------------------------------------------------------------------------


class TestGuardianAuditLog:
    """Guardian block events are recorded in the audit log."""

    def test_deny_event_logged_for_rm_rf(
        self, daemon_client: httpx.Client, mock_client: httpx.Client, audit_log_path: Path
    ):
        """After a blocked rm -rf command, the audit log contains a
        validate_action event with verdict=deny for tool exec."""
        send_message(daemon_client, "delete everything", sender_id="guardian-audit-1")

        entries = read_audit_entries(audit_log_path)
        deny_entries = [
            e for e in entries
            if e.get("event") == "validate_action"
            and e.get("verdict") == "deny"
            and e.get("tool_name") == "exec"
        ]
        assert len(deny_entries) > 0, (
            f"Expected at least one deny entry for exec in audit log, "
            f"got {len(entries)} total entries: "
            f"{[e.get('event') for e in entries]}"
        )

    def test_deny_event_has_matched_rule(
        self, daemon_client: httpx.Client, mock_client: httpx.Client, audit_log_path: Path
    ):
        """The deny audit entry includes the matched_rule that triggered the block."""
        send_message(daemon_client, "delete everything", sender_id="guardian-audit-2")

        entries = read_audit_entries(audit_log_path)
        deny_entries = [
            e for e in entries
            if e.get("event") == "validate_action"
            and e.get("verdict") == "deny"
            and e.get("tool_name") == "exec"
        ]
        assert len(deny_entries) > 0

        latest_deny = deny_entries[-1]
        assert "matched_rule" in latest_deny, "Deny entry missing matched_rule field"
        # The matched rule should reference the rm -rf pattern
        assert latest_deny["matched_rule"], "matched_rule is empty"

    def test_action_outcome_logged(
        self, daemon_client: httpx.Client, mock_client: httpx.Client, audit_log_path: Path
    ):
        """After a deny, an action_outcome event is logged with outcome=denied_by_policy."""
        send_message(daemon_client, "delete everything", sender_id="guardian-audit-3")

        entries = read_audit_entries(audit_log_path)
        outcome_entries = [
            e for e in entries
            if e.get("event") == "action_outcome"
            and e.get("outcome") == "denied_by_policy"
            and e.get("tool_name") == "exec"
        ]
        assert len(outcome_entries) > 0, (
            "Expected action_outcome with outcome=denied_by_policy in audit log"
        )

    def test_allow_event_logged_for_safe_command(
        self, daemon_client: httpx.Client, mock_client: httpx.Client, audit_log_path: Path
    ):
        """An allowed command also produces a validate_action audit entry
        with verdict=allow."""
        send_message(daemon_client, "run allowed echo", sender_id="guardian-audit-4")

        entries = read_audit_entries(audit_log_path)
        allow_entries = [
            e for e in entries
            if e.get("event") == "validate_action"
            and e.get("verdict") == "allow"
            and e.get("tool_name") == "exec"
        ]
        assert len(allow_entries) > 0, (
            "Expected at least one allow entry for exec in audit log"
        )
