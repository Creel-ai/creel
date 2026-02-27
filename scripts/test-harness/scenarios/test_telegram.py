"""HARNESS-005: Telegram webhook integration tests.

Tests the Telegram channel webhook flow end-to-end:
  - Text message via webhook → processed by agent → response sent via mock sendMessage
  - Photo with caption → caption extracted and processed
  - Unknown sender → silently ignored (not processed)
  - Malformed update → handled gracefully
  - Webhook secret validation → 403 on invalid secret
"""

from __future__ import annotations

import time

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEBHOOK_PATH = "/webhooks/telegram"
WEBHOOK_SECRET = "test-webhook-secret"
ALLOWED_SENDER_ID = "111222333"


def _make_text_update(
    text: str,
    sender_id: str = ALLOWED_SENDER_ID,
    chat_id: int | None = None,
    username: str = "testuser",
    chat_type: str = "private",
    update_id: int = 1,
    message_id: int = 1,
) -> dict:
    """Build a Telegram text message update."""
    if chat_id is None:
        chat_id = int(sender_id) if sender_id.isdigit() else 42
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {
                "id": int(sender_id) if sender_id.isdigit() else 0,
                "is_bot": False,
                "first_name": "Test",
                "username": username,
            },
            "chat": {
                "id": chat_id,
                "type": chat_type,
            },
            "date": int(time.time()),
            "text": text,
        },
    }


def _make_photo_update(
    caption: str = "",
    sender_id: str = ALLOWED_SENDER_ID,
    chat_id: int | None = None,
) -> dict:
    """Build a Telegram photo message update."""
    if chat_id is None:
        chat_id = int(sender_id) if sender_id.isdigit() else 42
    update = {
        "update_id": 2,
        "message": {
            "message_id": 2,
            "from": {
                "id": int(sender_id) if sender_id.isdigit() else 0,
                "is_bot": False,
                "first_name": "Test",
                "username": "testuser",
            },
            "chat": {
                "id": chat_id,
                "type": "private",
            },
            "date": int(time.time()),
            "photo": [
                {
                    "file_id": "AgACtest123",
                    "file_unique_id": "unique_test",
                    "width": 320,
                    "height": 240,
                    "file_size": 12345,
                },
            ],
        },
    }
    if caption:
        update["message"]["caption"] = caption
    return update


def _make_voice_update(
    sender_id: str = ALLOWED_SENDER_ID,
    chat_id: int | None = None,
) -> dict:
    """Build a Telegram voice message update (no text/caption)."""
    if chat_id is None:
        chat_id = int(sender_id) if sender_id.isdigit() else 42
    return {
        "update_id": 3,
        "message": {
            "message_id": 3,
            "from": {
                "id": int(sender_id) if sender_id.isdigit() else 0,
                "is_bot": False,
                "first_name": "Test",
                "username": "testuser",
            },
            "chat": {
                "id": chat_id,
                "type": "private",
            },
            "date": int(time.time()),
            "voice": {
                "file_id": "AwACvoice456",
                "file_unique_id": "unique_voice",
                "duration": 5,
                "mime_type": "audio/ogg",
                "file_size": 9876,
            },
        },
    }


def _post_webhook(
    client: httpx.Client,
    update: dict,
    secret: str = WEBHOOK_SECRET,
    include_secret: bool = True,
) -> httpx.Response:
    """POST a Telegram update to the webhook endpoint."""
    headers = {"Content-Type": "application/json"}
    if include_secret:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return client.post(WEBHOOK_PATH, json=update, headers=headers)


def _get_sent_messages(mock_client: httpx.Client) -> list[dict]:
    """Get all messages sent via mock Telegram sendMessage."""
    resp = mock_client.get("/v1/mock/telegram/messages")
    return resp.json().get("messages", [])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_telegram_state(mock_client: httpx.Client):
    """Reset mock Telegram state before each test."""
    mock_client.post("/v1/mock/telegram/reset")
    yield


# ---------------------------------------------------------------------------
# Tests: Text message processing
# ---------------------------------------------------------------------------


class TestTextMessageWebhook:
    """POST Telegram text update → agent processes → sendMessage called."""

    def test_text_message_returns_ok(self, daemon_client: httpx.Client):
        """Webhook returns {"status": "ok"} for a valid text message."""
        update = _make_text_update("hello")
        resp = _post_webhook(daemon_client, update)
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

    def test_text_message_triggers_send_message(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """A text message triggers the agent and sends a response via sendMessage."""
        update = _make_text_update("hello")
        _post_webhook(daemon_client, update)

        sent = _get_sent_messages(mock_client)
        assert len(sent) >= 1, f"Expected at least 1 sendMessage call, got {len(sent)}"

        # The sendMessage should be to the same chat_id
        last_msg = sent[-1]
        assert last_msg["params"]["chat_id"] == str(
            update["message"]["chat"]["id"]
        ), "sendMessage chat_id should match the incoming message's chat_id"

    def test_scripted_trigger_response(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """'hello' triggers the greeting response from mock LLM."""
        update = _make_text_update("hello")
        _post_webhook(daemon_client, update)

        sent = _get_sent_messages(mock_client)
        assert len(sent) >= 1
        # The greeting trigger returns "Hello! I'm the test agent."
        sent_text = sent[-1]["params"]["text"]
        assert "test agent" in sent_text.lower(), (
            f"Expected greeting response, got: {sent_text[:200]}"
        )

    def test_echo_fallback_response(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """A message that doesn't match specific triggers gets the echo response."""
        update = _make_text_update("some random message for echo test")
        _post_webhook(daemon_client, update)

        sent = _get_sent_messages(mock_client)
        assert len(sent) >= 1
        sent_text = sent[-1]["params"]["text"]
        assert "echo" in sent_text.lower(), (
            f"Expected echo response, got: {sent_text[:200]}"
        )

    def test_llm_receives_webhook_message(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """The mock LLM receives the text from the webhook message."""
        update = _make_text_update("hello from telegram webhook")
        _post_webhook(daemon_client, update)

        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1, "Expected at least 1 LLM call"

        # The first call should contain the webhook message text
        first_call = history["calls"][0]
        messages = first_call["body"].get("messages", [])
        user_texts = []
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    user_texts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            user_texts.append(block.get("text", ""))
        assert any("hello from telegram webhook" in t for t in user_texts), (
            f"Expected webhook text in LLM call, got user texts: {user_texts}"
        )


# ---------------------------------------------------------------------------
# Tests: Photo with caption
# ---------------------------------------------------------------------------


class TestPhotoCaption:
    """Photo messages: caption is extracted as text and processed."""

    def test_photo_with_caption_processed(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """A photo with caption uses the caption as the message text."""
        update = _make_photo_update(caption="describe this image")
        _post_webhook(daemon_client, update)

        sent = _get_sent_messages(mock_client)
        assert len(sent) >= 1, (
            "Expected sendMessage after photo with caption"
        )

    def test_photo_without_caption_ignored(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """A photo without caption or text is silently ignored (no sendMessage)."""
        update = _make_photo_update(caption="")
        _post_webhook(daemon_client, update)

        sent = _get_sent_messages(mock_client)
        assert len(sent) == 0, (
            f"Expected no sendMessage for photo without caption, got {len(sent)}"
        )

    def test_photo_without_caption_returns_ok(self, daemon_client: httpx.Client):
        """Webhook still returns ok for photo without caption."""
        update = _make_photo_update(caption="")
        resp = _post_webhook(daemon_client, update)
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"


# ---------------------------------------------------------------------------
# Tests: Voice without text
# ---------------------------------------------------------------------------


class TestVoiceMessage:
    """Voice messages: without text/caption, they are not processed."""

    def test_voice_without_text_ignored(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """A voice message with no text or caption is silently ignored."""
        update = _make_voice_update()
        _post_webhook(daemon_client, update)

        sent = _get_sent_messages(mock_client)
        assert len(sent) == 0, (
            f"Expected no sendMessage for voice without text, got {len(sent)}"
        )


# ---------------------------------------------------------------------------
# Tests: Unknown sender filtering
# ---------------------------------------------------------------------------


class TestUnknownSender:
    """Messages from senders not in allowed_senders are silently ignored."""

    def test_unknown_sender_ignored(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """A message from an unknown sender is not processed."""
        update = _make_text_update(
            "hello",
            sender_id="999888777",
            username="unknown_user",
            chat_id=999888777,
        )
        _post_webhook(daemon_client, update)

        sent = _get_sent_messages(mock_client)
        assert len(sent) == 0, (
            f"Expected no sendMessage for unknown sender, got {len(sent)}"
        )

    def test_unknown_sender_returns_ok(self, daemon_client: httpx.Client):
        """Webhook returns ok even for unknown senders (no error exposed)."""
        update = _make_text_update(
            "hello",
            sender_id="999888777",
            username="unknown_user",
            chat_id=999888777,
        )
        resp = _post_webhook(daemon_client, update)
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

    def test_unknown_sender_no_llm_call(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """Unknown sender should not trigger any LLM call."""
        update = _make_text_update(
            "hello",
            sender_id="999888777",
            username="unknown_user",
            chat_id=999888777,
        )
        _post_webhook(daemon_client, update)

        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) == 0, (
            f"Expected no LLM calls for unknown sender, got {len(history['calls'])}"
        )


# ---------------------------------------------------------------------------
# Tests: Malformed updates
# ---------------------------------------------------------------------------


class TestMalformedUpdates:
    """Malformed or missing fields in the update are handled gracefully."""

    def test_no_message_field_returns_ok(self, daemon_client: httpx.Client):
        """An update without a 'message' field returns ok (no-op)."""
        resp = _post_webhook(daemon_client, {"update_id": 100})
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

    def test_empty_body_handled(self, daemon_client: httpx.Client):
        """An empty JSON body returns ok or is handled without crashing."""
        resp = _post_webhook(daemon_client, {})
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

    def test_missing_text_and_caption_returns_ok(self, daemon_client: httpx.Client):
        """A message with no text and no caption returns ok (no-op)."""
        update = {
            "update_id": 101,
            "message": {
                "message_id": 10,
                "from": {"id": int(ALLOWED_SENDER_ID), "username": "testuser"},
                "chat": {"id": int(ALLOWED_SENDER_ID), "type": "private"},
                "date": int(time.time()),
                # No text, no caption
            },
        }
        resp = _post_webhook(daemon_client, update)
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"


# ---------------------------------------------------------------------------
# Tests: Webhook secret validation
# ---------------------------------------------------------------------------


class TestWebhookSecret:
    """Webhook secret token validation via X-Telegram-Bot-Api-Secret-Token."""

    def test_valid_secret_accepted(self, daemon_client: httpx.Client):
        """Valid secret token allows the request through."""
        update = _make_text_update("hello")
        resp = _post_webhook(daemon_client, update, secret=WEBHOOK_SECRET)
        assert resp.status_code == 200

    def test_invalid_secret_rejected(self, daemon_client: httpx.Client):
        """Invalid secret token returns 403."""
        update = _make_text_update("hello")
        resp = _post_webhook(daemon_client, update, secret="wrong-secret")
        assert resp.status_code == 403

    def test_missing_secret_rejected(self, daemon_client: httpx.Client):
        """Missing secret token header returns 403."""
        update = _make_text_update("hello")
        resp = _post_webhook(daemon_client, update, include_secret=False)
        assert resp.status_code == 403
