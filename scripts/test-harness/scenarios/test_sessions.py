"""HARNESS-008: Session persistence and isolation integration tests.

Tests that sessions are properly isolated per sender, history is retrievable
via the API, conversation context is maintained across messages, and /clear
only affects the targeted sender's session.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import send_message


class TestSeparateSessions:
    """Messages from distinct senders create separate sessions."""

    def test_two_senders_get_different_session_ids(self, daemon_client: httpx.Client):
        """Sender A and Sender B each receive a distinct session_id."""
        sender_a = "session-iso-A"
        sender_b = "session-iso-B"

        resp_a = send_message(daemon_client, "hello from A", sender_id=sender_a)
        assert resp_a.status_code == 200
        sid_a = resp_a.json()["session_id"]
        assert sid_a

        resp_b = send_message(daemon_client, "hello from B", sender_id=sender_b)
        assert resp_b.status_code == 200
        sid_b = resp_b.json()["session_id"]
        assert sid_b

        assert sid_a != sid_b, "Senders A and B should have different session IDs"

    def test_sender_session_stable_across_messages(self, daemon_client: httpx.Client):
        """Consecutive messages from the same sender reuse the same session."""
        sender = "session-stable"

        resp1 = send_message(daemon_client, "first message", sender_id=sender)
        assert resp1.status_code == 200
        sid1 = resp1.json()["session_id"]

        resp2 = send_message(daemon_client, "second message", sender_id=sender)
        assert resp2.status_code == 200
        sid2 = resp2.json()["session_id"]

        assert sid1 == sid2, "Same sender should keep the same active session"


class TestSessionHistoryPerSender:
    """Session history API returns correct messages per sender."""

    def test_history_contains_user_and_assistant(self, daemon_client: httpx.Client):
        """Session history has both user and assistant messages."""
        sender = "session-hist-1"
        resp = send_message(daemon_client, "test history content", sender_id=sender)
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        hist = daemon_client.get(
            f"/v1/sessions/{sid}/history",
            params={"sender_id": sender},
        )
        assert hist.status_code == 200
        messages = hist.json()["messages"]
        roles = [m["role"] for m in messages]
        assert "user" in roles
        assert "assistant" in roles

    def test_history_reflects_multiple_exchanges(self, daemon_client: httpx.Client):
        """After two exchanges, session history contains messages from both rounds."""
        sender = "session-hist-multi"

        resp1 = send_message(daemon_client, "first exchange", sender_id=sender)
        assert resp1.status_code == 200
        sid = resp1.json()["session_id"]

        resp2 = send_message(daemon_client, "second exchange", sender_id=sender)
        assert resp2.status_code == 200

        hist = daemon_client.get(
            f"/v1/sessions/{sid}/history",
            params={"sender_id": sender},
        )
        assert hist.status_code == 200
        messages = hist.json()["messages"]
        user_msgs = [m for m in messages if m["role"] == "user"]
        # At least 2 user messages from the two exchanges
        assert len(user_msgs) >= 2

    def test_history_isolated_between_senders(self, daemon_client: httpx.Client):
        """Sender A's history does not include Sender B's messages."""
        sender_a = "session-hist-iso-A"
        sender_b = "session-hist-iso-B"

        resp_a = send_message(daemon_client, "A says PINEAPPLE", sender_id=sender_a)
        assert resp_a.status_code == 200
        sid_a = resp_a.json()["session_id"]

        resp_b = send_message(daemon_client, "B says MANGO", sender_id=sender_b)
        assert resp_b.status_code == 200
        sid_b = resp_b.json()["session_id"]

        # A's history should have PINEAPPLE but not MANGO
        hist_a = daemon_client.get(
            f"/v1/sessions/{sid_a}/history",
            params={"sender_id": sender_a},
        ).json()
        a_content = " ".join(str(m.get("content", "")) for m in hist_a["messages"])
        assert "PINEAPPLE" in a_content
        assert "MANGO" not in a_content

        # B's history should have MANGO but not PINEAPPLE
        hist_b = daemon_client.get(
            f"/v1/sessions/{sid_b}/history",
            params={"sender_id": sender_b},
        ).json()
        b_content = " ".join(str(m.get("content", "")) for m in hist_b["messages"])
        assert "MANGO" in b_content
        assert "PINEAPPLE" not in b_content


class TestSessionContextMaintained:
    """Mock LLM receives full history on subsequent messages."""

    def test_llm_receives_prior_history(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """On the second message, the mock LLM call includes the first message."""
        sender = "session-ctx-1"

        # First message — establishes context
        resp1 = send_message(daemon_client, "remember the word ZEBRA", sender_id=sender)
        assert resp1.status_code == 200

        # Reset mock history to isolate the second call
        mock_client.post("/v1/mock/reset")

        # Second message
        resp2 = send_message(daemon_client, "what was the word?", sender_id=sender)
        assert resp2.status_code == 200

        # Verify the mock LLM received the prior ZEBRA message in its history
        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1
        last_call = history["calls"][-1]
        messages = last_call["body"].get("messages", [])
        all_content = " ".join(str(m.get("content", "")) for m in messages)
        assert "ZEBRA" in all_content, (
            f"Prior message 'ZEBRA' not found in LLM history: {all_content[:300]}"
        )

    def test_third_message_includes_full_conversation(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """After three messages, the LLM receives the complete conversation history."""
        sender = "session-ctx-3msg"

        send_message(daemon_client, "my pet is a PARROT", sender_id=sender)
        send_message(daemon_client, "it is GREEN", sender_id=sender)

        mock_client.post("/v1/mock/reset")
        send_message(daemon_client, "describe my pet", sender_id=sender)

        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1
        last_call = history["calls"][-1]
        messages = last_call["body"].get("messages", [])
        all_content = " ".join(str(m.get("content", "")) for m in messages)
        assert "PARROT" in all_content, "First message not in LLM context"
        assert "GREEN" in all_content, "Second message not in LLM context"


class TestClearIsolation:
    """/clear resets one sender's session without affecting the other."""

    def test_clear_only_affects_target_sender(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """Clearing Sender A's session does not erase Sender B's history."""
        sender_a = "session-clear-iso-A"
        sender_b = "session-clear-iso-B"

        # Both senders establish history
        resp_a = send_message(daemon_client, "A remembers DIAMOND", sender_id=sender_a)
        assert resp_a.status_code == 200
        sid_a = resp_a.json()["session_id"]

        resp_b = send_message(daemon_client, "B remembers EMERALD", sender_id=sender_b)
        assert resp_b.status_code == 200
        sid_b = resp_b.json()["session_id"]

        # Clear sender A's session
        clear_resp = send_message(daemon_client, "/clear", sender_id=sender_a)
        assert clear_resp.status_code == 200

        # Sender A's history should be cleared (empty or minimal)
        hist_a = daemon_client.get(
            f"/v1/sessions/{sid_a}/history",
            params={"sender_id": sender_a},
        ).json()
        a_content = " ".join(str(m.get("content", "")) for m in hist_a["messages"])
        assert "DIAMOND" not in a_content, "Sender A's history should be cleared"

        # Sender B's history should still have EMERALD
        hist_b = daemon_client.get(
            f"/v1/sessions/{sid_b}/history",
            params={"sender_id": sender_b},
        ).json()
        b_content = " ".join(str(m.get("content", "")) for m in hist_b["messages"])
        assert "EMERALD" in b_content, "Sender B's history should be unaffected"

    def test_clear_sender_llm_context_reset(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """After /clear, the LLM no longer receives the cleared sender's prior messages."""
        sender = "session-clear-ctx"

        # Establish history
        send_message(daemon_client, "secret password is TOPAZ", sender_id=sender)

        # Clear
        send_message(daemon_client, "/clear", sender_id=sender)

        # Reset mock and send new message
        mock_client.post("/v1/mock/reset")
        send_message(daemon_client, "what was the password?", sender_id=sender)

        # Verify LLM doesn't see TOPAZ
        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1
        last_call = history["calls"][-1]
        messages = last_call["body"].get("messages", [])
        all_content = " ".join(str(m.get("content", "")) for m in messages)
        assert "TOPAZ" not in all_content, (
            "Cleared history still visible to LLM"
        )


class TestSessionListAPI:
    """GET /v1/sessions returns all sessions for a sender."""

    def test_session_list_shows_active_session(self, daemon_client: httpx.Client):
        """After sending a message, the session appears in the session list."""
        sender = "session-list-1"

        resp = send_message(daemon_client, "list test", sender_id=sender)
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        sessions_resp = daemon_client.get("/v1/sessions", params={"sender_id": sender})
        assert sessions_resp.status_code == 200
        sessions = sessions_resp.json()
        session_ids = [s["session_id"] for s in sessions]
        assert sid in session_ids

    def test_session_list_includes_metadata(self, daemon_client: httpx.Client):
        """Session list entries include expected metadata fields."""
        sender = "session-list-meta"

        resp = send_message(daemon_client, "metadata test msg", sender_id=sender)
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        sessions_resp = daemon_client.get("/v1/sessions", params={"sender_id": sender})
        assert sessions_resp.status_code == 200
        sessions = sessions_resp.json()
        session = next(s for s in sessions if s["session_id"] == sid)

        assert "created_at" in session
        assert "last_active" in session
        assert "message_count" in session
        assert session["message_count"] >= 2  # at least user + assistant

    def test_session_list_scoped_to_sender(self, daemon_client: httpx.Client):
        """Session list for one sender does not include another sender's sessions."""
        sender_x = "session-list-X"
        sender_y = "session-list-Y"

        resp_x = send_message(daemon_client, "X data", sender_id=sender_x)
        assert resp_x.status_code == 200
        sid_x = resp_x.json()["session_id"]

        resp_y = send_message(daemon_client, "Y data", sender_id=sender_y)
        assert resp_y.status_code == 200
        sid_y = resp_y.json()["session_id"]

        # X's session list should include X's session but not Y's
        sessions_x = daemon_client.get(
            "/v1/sessions", params={"sender_id": sender_x}
        ).json()
        x_ids = [s["session_id"] for s in sessions_x]
        assert sid_x in x_ids
        assert sid_y not in x_ids

        # Y's session list should include Y's session but not X's
        sessions_y = daemon_client.get(
            "/v1/sessions", params={"sender_id": sender_y}
        ).json()
        y_ids = [s["session_id"] for s in sessions_y]
        assert sid_y in y_ids
        assert sid_x not in y_ids

    def test_active_session_endpoint(self, daemon_client: httpx.Client):
        """GET /v1/sessions/active returns the current active session."""
        sender = "session-active-ep"

        resp = send_message(daemon_client, "active test", sender_id=sender)
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        active_resp = daemon_client.get(
            "/v1/sessions/active", params={"sender_id": sender}
        )
        assert active_resp.status_code == 200
        active = active_resp.json()
        assert active["session_id"] == sid
        assert active["sender_id"] == sender
