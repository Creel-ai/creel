"""Tests for Telegram bridge implementation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creel.channels.telegram_bridge import (
    HttpTelegramBridge,
    TelegramMessage,
    _chunk_text,
)


@pytest.fixture
def bridge():
    return HttpTelegramBridge("fake-token-123")


class TestSendMessage:
    def test_send_message_under_limit(self, bridge):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=MagicMock(return_value={"ok": True, "result": {}})
            )
            bridge.send_message("12345", "hello")
            assert mock_post.call_count == 1
            call_kwargs = mock_post.call_args
            assert call_kwargs[1]["json"]["chat_id"] == "12345"
            assert call_kwargs[1]["json"]["text"] == "hello"

    def test_send_message_over_limit(self, bridge):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=MagicMock(return_value={"ok": True, "result": {}})
            )
            long_text = "a" * 5000
            bridge.send_message("12345", long_text)
            assert mock_post.call_count == 2


class TestGetMe:
    def test_get_me_caches_result(self, bridge):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=MagicMock(return_value={
                    "ok": True,
                    "result": {"id": 111, "username": "testbot"},
                })
            )
            result1 = bridge.get_me()
            result2 = bridge.get_me()
            assert result1 == {"id": 111, "username": "testbot"}
            assert result1 == result2
            # Only one API call despite two get_me() calls
            assert mock_post.call_count == 1


class TestGetUpdates:
    def test_get_updates_parses_messages(self, bridge):
        api_response = {
            "ok": True,
            "result": [
                {
                    "update_id": 100,
                    "message": {
                        "message_id": 1,
                        "from": {"id": 42, "username": "alice"},
                        "chat": {"id": 42, "type": "private"},
                        "text": "hello bot",
                    },
                }
            ],
        }
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=MagicMock(return_value=api_response)
            )
            messages = bridge.get_updates(offset=None, timeout=10)
            assert len(messages) == 1
            msg = messages[0]
            assert isinstance(msg, TelegramMessage)
            assert msg.sender_id == "42"
            assert msg.sender_username == "alice"
            assert msg.chat_id == "42"
            assert msg.text == "hello bot"
            assert msg.update_id == 100
            assert msg.is_group is False

    def test_get_updates_handles_media(self, bridge):
        api_response = {
            "ok": True,
            "result": [
                {
                    "update_id": 101,
                    "message": {
                        "message_id": 2,
                        "from": {"id": 42, "username": "alice"},
                        "chat": {"id": 42, "type": "private"},
                        "caption": "check this photo",
                        "photo": [{"file_id": "abc123", "width": 100, "height": 100}],
                    },
                }
            ],
        }
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=MagicMock(return_value=api_response)
            )
            messages = bridge.get_updates()
            assert len(messages) == 1
            assert messages[0].text == "check this photo"


class TestApiError:
    def test_api_error_raises(self, bridge):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=MagicMock(return_value={
                    "ok": False,
                    "description": "Unauthorized",
                })
            )
            with pytest.raises(RuntimeError, match="Unauthorized"):
                bridge.get_me()


class TestHealth:
    def test_health_returns_healthy(self, bridge):
        with patch("httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                json=MagicMock(return_value={
                    "ok": True,
                    "result": {"id": 111, "username": "testbot"},
                })
            )
            status = bridge.health()
            assert status["healthy"] is True

    def test_health_returns_unhealthy_on_error(self, bridge):
        with patch("httpx.post") as mock_post:
            mock_post.side_effect = RuntimeError("connection failed")
            status = bridge.health()
            assert status["healthy"] is False
            assert "error" in status


class TestChunkText:
    def test_short_text_single_chunk(self):
        assert _chunk_text("hello", 4096) == ["hello"]

    def test_long_text_splits_at_newline(self):
        text = "line1\n" * 1000  # ~6000 chars
        chunks = _chunk_text(text, 4096)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 4096

    def test_long_text_no_newlines(self):
        text = "a" * 5000
        chunks = _chunk_text(text, 4096)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 4096
        assert chunks[1] == "a" * 904
