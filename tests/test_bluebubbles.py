"""Tests for BlueBubbles fetcher — security enforcement and API integration."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from fetchers.bluebubbles.fetcher import (
    MAX_MESSAGE_LENGTH,
    MAX_MESSAGES_PER_REQUEST,
    VALID_REACTIONS,
    _send_timestamps,
    get_chats,
    get_recent_messages,
    send_message,
    send_reaction,
)

SERVER = "http://localhost:1234"
PASSWORD = "test-password"
ALLOWED_RECIPIENTS = {"iMessage;-;+11234567890", "iMessage;-;+10987654321"}
ALLOWED_CHATS = {"iMessage;-;+11234567890"}


def _mock_api_response(data):
    """Create a mock requests.Response."""
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"data": data}
    mock.raise_for_status = MagicMock()
    return mock


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    """Reset rate limiter between tests."""
    _send_timestamps.clear()
    yield
    _send_timestamps.clear()


# --- Recipient allowlist ---

def test_send_message_blocked_recipient():
    """Sending to a recipient not in the allowlist raises an error."""
    with pytest.raises(RuntimeError, match="not in allowlist"):
        send_message(SERVER, PASSWORD, ALLOWED_RECIPIENTS, "iMessage;-;+19999999999", "hello")


def test_send_message_empty_allowlist():
    """Sending with an empty allowlist raises an error."""
    with pytest.raises(RuntimeError, match="No allowed recipients"):
        send_message(SERVER, PASSWORD, set(), "iMessage;-;+11234567890", "hello")


def test_send_reaction_blocked_recipient():
    """Reacting to a chat not in the allowlist raises an error."""
    with pytest.raises(RuntimeError, match="not in allowlist"):
        send_reaction(SERVER, PASSWORD, ALLOWED_RECIPIENTS, "iMessage;-;+19999999999", "guid", "love")


# --- Message cap enforcement ---

@patch("fetchers.bluebubbles.fetcher.requests.request")
def test_message_cap_enforced(mock_req):
    """Requesting more than MAX_MESSAGES_PER_REQUEST returns at most the cap."""
    mock_req.return_value = _mock_api_response([])

    get_recent_messages(SERVER, PASSWORD, set(), limit=100)

    # Verify the API was called with the capped limit
    call_kwargs = mock_req.call_args
    params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
    assert params["limit"] == MAX_MESSAGES_PER_REQUEST


# --- Rate limiting ---

def test_rate_limit_enforced():
    """Exceeding send rate limit raises an error."""
    import time

    # Fill up the rate limiter
    now = time.time()
    _send_timestamps.extend([now] * 10)

    with pytest.raises(RuntimeError, match="Rate limit exceeded"):
        send_message(SERVER, PASSWORD, ALLOWED_RECIPIENTS, "iMessage;-;+11234567890", "hello")


# --- Message length ---

def test_message_length_enforced():
    """Messages exceeding MAX_MESSAGE_LENGTH are rejected."""
    long_text = "x" * (MAX_MESSAGE_LENGTH + 1)
    with pytest.raises(RuntimeError, match="Message too long"):
        send_message(SERVER, PASSWORD, ALLOWED_RECIPIENTS, "iMessage;-;+11234567890", long_text)


# --- Reaction validation ---

def test_invalid_reaction_rejected():
    """Invalid reaction types are rejected."""
    with pytest.raises(RuntimeError, match="Invalid reaction"):
        send_reaction(SERVER, PASSWORD, ALLOWED_RECIPIENTS, "iMessage;-;+11234567890", "guid", "thumbsup")


# --- Successful operations (mocked HTTP) ---

@patch("fetchers.bluebubbles.fetcher.requests.request")
def test_send_message_allowed(mock_req):
    """Sending to an allowed recipient succeeds."""
    mock_req.return_value = _mock_api_response({"guid": "msg-123"})

    result = send_message(SERVER, PASSWORD, ALLOWED_RECIPIENTS, "iMessage;-;+11234567890", "Hello!")

    assert result["status"] == "sent"
    assert result["chat_id"] == "iMessage;-;+11234567890"
    mock_req.assert_called_once()


@patch("fetchers.bluebubbles.fetcher.requests.request")
def test_get_recent_messages_format(mock_req):
    """get_recent_messages returns expected format."""
    mock_req.return_value = _mock_api_response([
        {
            "text": "Hey there",
            "handle": {"address": "+11234567890"},
            "chats": [{"chatIdentifier": "iMessage;-;+11234567890"}],
            "dateCreated": "2026-01-01T00:00:00Z",
            "isFromMe": False,
        },
    ])

    result = get_recent_messages(SERVER, PASSWORD, ALLOWED_CHATS)

    assert len(result) == 1
    msg = result[0]
    assert msg["sender"] == "+11234567890"
    assert msg["text"] == "Hey there"
    assert msg["chat_id"] == "iMessage;-;+11234567890"
    assert msg["is_from_me"] is False
    # Ensure no message GUID/ID is exposed
    assert "guid" not in msg
    assert "id" not in msg


@patch("fetchers.bluebubbles.fetcher.requests.request")
def test_get_recent_messages_filters_disallowed_chats(mock_req):
    """Messages from chats not in allowed_chats are filtered out."""
    mock_req.return_value = _mock_api_response([
        {
            "text": "Allowed",
            "handle": {"address": "+11234567890"},
            "chats": [{"chatIdentifier": "iMessage;-;+11234567890"}],
            "dateCreated": "2026-01-01T00:00:00Z",
            "isFromMe": False,
        },
        {
            "text": "Not allowed",
            "handle": {"address": "+19999999999"},
            "chats": [{"chatIdentifier": "iMessage;-;+19999999999"}],
            "dateCreated": "2026-01-01T00:00:00Z",
            "isFromMe": False,
        },
    ])

    result = get_recent_messages(SERVER, PASSWORD, ALLOWED_CHATS)
    assert len(result) == 1
    assert result[0]["text"] == "Allowed"


@patch("fetchers.bluebubbles.fetcher.requests.request")
def test_get_chats(mock_req):
    """get_chats returns expected format."""
    mock_req.return_value = _mock_api_response([
        {
            "chatIdentifier": "iMessage;-;+11234567890",
            "displayName": "John",
            "lastMessage": {"dateCreated": "2026-01-01T00:00:00Z"},
        },
    ])

    result = get_chats(SERVER, PASSWORD, ALLOWED_CHATS)
    assert len(result) == 1
    assert result[0]["chat_id"] == "iMessage;-;+11234567890"
    assert result[0]["display_name"] == "John"


@patch("fetchers.bluebubbles.fetcher.requests.request")
def test_send_reaction_valid(mock_req):
    """Valid reaction to allowed recipient succeeds."""
    mock_req.return_value = _mock_api_response({})

    result = send_reaction(
        SERVER, PASSWORD, ALLOWED_RECIPIENTS,
        "iMessage;-;+11234567890", "msg-guid-123", "love",
    )
    assert result["status"] == "reacted"
    assert result["reaction"] == "love"
