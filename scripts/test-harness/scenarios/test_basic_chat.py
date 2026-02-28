"""HARNESS-003: Basic chat integration tests.

Tests the fundamental message -> LLM -> response pipeline via the daemon API.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import send_message


class TestSimpleChat:
    """POST /v1/messages returns 200 with agent reply."""

    def test_greeting_returns_scripted_response(self, daemon_client: httpx.Client):
        """Sending 'hello' matches the greeting trigger and returns the scripted reply."""
        resp = send_message(daemon_client, "hello")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sender_id"] == "test-sender-A"
        assert "Hello" in data["text"] or "hello" in data["text"].lower()
        assert data["session_id"]  # a session should have been created

    def test_echo_fallback(self, daemon_client: httpx.Client):
        """A message that matches no specific trigger falls through to the echo catch-all."""
        resp = send_message(daemon_client, "this is a unique test message 12345")
        assert resp.status_code == 200
        data = resp.json()
        # The .* catch-all trigger returns "Echo: {user_message}"
        assert "this is a unique test message 12345" in data["text"]

    def test_response_contains_mock_llm_text(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """The agent response text comes from the mock LLM (verifiable via history)."""
        resp = send_message(daemon_client, "hello")
        assert resp.status_code == 200

        # Verify the mock LLM was actually called
        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1
        # The last user message in the LLM call should contain "hello"
        last_call = history["calls"][-1]
        messages = last_call["body"].get("messages", [])
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert any("hello" in str(m.get("content", "")) for m in user_msgs)


class TestSessionCreation:
    """Sending a message creates a session for the sender."""

    def test_session_created_after_message(self, daemon_client: httpx.Client):
        """After sending a message, a session exists for the sender."""
        sender = "test-session-create"
        resp = send_message(daemon_client, "hello", sender_id=sender)
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]
        assert session_id

        # Verify session appears in the session list
        sessions_resp = daemon_client.get("/v1/sessions", params={"sender_id": sender})
        assert sessions_resp.status_code == 200
        sessions = sessions_resp.json()
        session_ids = [s["session_id"] for s in sessions]
        assert session_id in session_ids

    def test_session_has_message_history(self, daemon_client: httpx.Client):
        """The session history contains the exchanged messages."""
        sender = "test-session-history"
        resp = send_message(daemon_client, "hello", sender_id=sender)
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

        hist_resp = daemon_client.get(
            f"/v1/sessions/{session_id}/history",
            params={"sender_id": sender},
        )
        assert hist_resp.status_code == 200
        messages = hist_resp.json()["messages"]
        # Should have at least user + assistant messages
        roles = [m["role"] for m in messages]
        assert "user" in roles
        assert "assistant" in roles


class TestConversationContext:
    """Multiple messages maintain conversation context (mock LLM receives history)."""

    def test_second_message_includes_prior_history(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """On the second message, the mock LLM receives the full conversation history."""
        sender = "test-context"

        # First message
        resp1 = send_message(daemon_client, "my name is Alice", sender_id=sender)
        assert resp1.status_code == 200

        # Reset mock history so we can isolate the second call
        mock_client.post("/v1/mock/reset")

        # Second message
        resp2 = send_message(daemon_client, "what did I say?", sender_id=sender)
        assert resp2.status_code == 200

        # Check mock LLM received prior history
        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1
        last_call = history["calls"][-1]
        messages = last_call["body"].get("messages", [])
        # Flatten content to strings for checking
        all_content = " ".join(str(m.get("content", "")) for m in messages)
        assert "Alice" in all_content, (
            f"Prior message about Alice not found in LLM history: {all_content[:300]}"
        )


class TestClearCommand:
    """The /clear command resets session history."""

    def test_clear_resets_history(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """After /clear, the LLM no longer receives prior conversation messages."""
        sender = "test-clear"

        # Build up some history
        resp1 = send_message(daemon_client, "my favourite colour is blue", sender_id=sender)
        assert resp1.status_code == 200
        session_id = resp1.json()["session_id"]

        # Clear the session
        clear_resp = send_message(daemon_client, "/clear", sender_id=sender)
        assert clear_resp.status_code == 200
        assert "clear" in clear_resp.json()["text"].lower() or "session" in clear_resp.json()["text"].lower()

        # Reset mock history
        mock_client.post("/v1/mock/reset")

        # Send a new message
        resp2 = send_message(daemon_client, "what is my favourite colour?", sender_id=sender)
        assert resp2.status_code == 200

        # Verify the LLM did NOT receive the prior "blue" message
        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1
        last_call = history["calls"][-1]
        messages = last_call["body"].get("messages", [])
        all_content = " ".join(str(m.get("content", "")) for m in messages)
        assert "blue" not in all_content, (
            "Prior message about 'blue' found in LLM history after /clear"
        )


class TestEmptyAndEdgeCases:
    """Edge cases: empty message, graceful error handling."""

    def test_empty_message_returns_error_or_handled(self, daemon_client: httpx.Client):
        """An empty message body is handled gracefully — 422 validation or a fallback response."""
        resp = daemon_client.post(
            "/v1/messages",
            json={"sender_id": "test-empty", "text": ""},
        )
        # Pydantic min_length=1 on text should return 422 Unprocessable Entity
        assert resp.status_code == 422

    def test_whitespace_only_message(self, daemon_client: httpx.Client):
        """A whitespace-only message is still accepted (min_length=1 passes with a space)."""
        resp = send_message(daemon_client, " ", sender_id="test-whitespace")
        # Should either process or be rejected gracefully
        assert resp.status_code in (200, 422)


class TestConcurrentSenders:
    """Messages from different senders are isolated in separate sessions."""

    def test_two_senders_have_separate_sessions(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """Sender A and Sender B each get their own session with isolated history."""
        sender_a = "concurrent-sender-A"
        sender_b = "concurrent-sender-B"

        # Sender A speaks
        resp_a = send_message(daemon_client, "I am Alice", sender_id=sender_a)
        assert resp_a.status_code == 200
        session_a = resp_a.json()["session_id"]

        # Sender B speaks
        resp_b = send_message(daemon_client, "I am Bob", sender_id=sender_b)
        assert resp_b.status_code == 200
        session_b = resp_b.json()["session_id"]

        # Sessions should be different
        assert session_a != session_b

        # Verify session isolation: A's history has Alice, not Bob
        hist_a = daemon_client.get(
            f"/v1/sessions/{session_a}/history",
            params={"sender_id": sender_a},
        ).json()
        a_content = " ".join(str(m.get("content", "")) for m in hist_a["messages"])
        assert "Alice" in a_content
        assert "Bob" not in a_content

        # B's history has Bob, not Alice
        hist_b = daemon_client.get(
            f"/v1/sessions/{session_b}/history",
            params={"sender_id": sender_b},
        ).json()
        b_content = " ".join(str(m.get("content", "")) for m in hist_b["messages"])
        assert "Bob" in b_content
        assert "Alice" not in b_content

    def test_sender_b_llm_call_does_not_include_sender_a_context(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """When Sender B messages, the LLM only sees Sender B's history."""
        sender_a = "isolation-sender-A"
        sender_b = "isolation-sender-B"

        # Sender A sends a distinctive message
        send_message(daemon_client, "secret code: ALPHA-7", sender_id=sender_a)
        mock_client.post("/v1/mock/reset")

        # Sender B sends a message
        send_message(daemon_client, "tell me a secret", sender_id=sender_b)

        # Check that the LLM call for sender B does NOT contain sender A's data
        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1
        last_call = history["calls"][-1]
        messages = last_call["body"].get("messages", [])
        all_content = " ".join(str(m.get("content", "")) for m in messages)
        assert "ALPHA-7" not in all_content, (
            "Sender A's message leaked into Sender B's LLM context"
        )
