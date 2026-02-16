"""Tests for daemon TUI client adapter and DaemonApiClient."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.daemon.client import DaemonApiClient, DaemonTuiAdapter


# ---------------------------------------------------------------------------
# Shared fake client for adapter tests
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.active = "sess-1"
        self.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]

    def send_message(self, sender_id: str, text: str, session_id: str | None = None):
        return {
            "sender_id": sender_id,
            "text": f"echo:{text}",
            "session_id": self.active,
        }

    def stream_message(self, sender_id: str, text: str, session_id: str | None = None):
        del session_id
        yield {
            "type": "start",
            "sender_id": sender_id,
            "session_id": self.active,
            "payload": {},
        }
        yield {
            "type": "token",
            "sender_id": sender_id,
            "session_id": self.active,
            "payload": {"text": "echo:"},
        }
        yield {
            "type": "token",
            "sender_id": sender_id,
            "session_id": self.active,
            "payload": {"text": text},
        }
        yield {
            "type": "final",
            "sender_id": sender_id,
            "session_id": self.active,
            "payload": {"text": f"echo:{text}"},
        }

    def get_active_session(self, sender_id: str):
        return {
            "sender_id": sender_id,
            "session_id": self.active,
            "title": "Test session",
            "created_at": 0.0,
            "last_active": 0.0,
            "message_count": len(self.messages),
        }

    def get_history(self, sender_id: str, session_id: str, limit: int = 100):
        return list(self.messages)[-limit:]

    def new_session(self, sender_id: str):
        self.active = "sess-2"
        self.messages = []
        return {
            "sender_id": sender_id,
            "session_id": self.active,
            "title": "",
            "created_at": 1.0,
            "last_active": 1.0,
            "message_count": 0,
        }

    def resume_session(self, sender_id: str, session_id: str):
        self.active = session_id
        return {
            "sender_id": sender_id,
            "session_id": self.active,
            "title": "Resumed",
            "created_at": 2.0,
            "last_active": 2.0,
            "message_count": len(self.messages),
        }

    def list_sessions(self, sender_id: str):
        return [
            {"session_id": self.active, "title": "A", "message_count": 1},
            {"session_id": "sess-x", "title": "B", "message_count": 2},
        ]


# ---------------------------------------------------------------------------
# DaemonTuiAdapter tests (existing)
# ---------------------------------------------------------------------------


def test_adapter_fetches_active_session_and_history() -> None:
    client = _FakeClient()
    adapter = DaemonTuiAdapter(client, sender_id="cli")

    session = adapter.get_or_create_session("cli")
    assert session.session_id == "sess-1"
    assert session.title == "Test session"
    assert session.messages and len(session.messages) == 2


def test_adapter_send_updates_active_session_id() -> None:
    client = _FakeClient()
    adapter = DaemonTuiAdapter(client, sender_id="cli")

    result = adapter.handle_message("cli", "hello")
    assert result == "echo:hello"


def test_adapter_new_and_resume_session() -> None:
    client = _FakeClient()
    adapter = DaemonTuiAdapter(client, sender_id="cli")

    new_session = adapter.new_session("cli")
    assert new_session.session_id == "sess-2"

    resumed = adapter.resume_session("cli", "sess-9")
    assert resumed.session_id == "sess-9"


def test_adapter_stream_updates_active_session_id() -> None:
    client = _FakeClient()
    adapter = DaemonTuiAdapter(client, sender_id="cli")

    events = list(adapter.stream_message("cli", "hello"))
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "final"


# ---------------------------------------------------------------------------
# DaemonTuiAdapter: list_sessions_text
# ---------------------------------------------------------------------------


def test_list_sessions_text_empty() -> None:
    class _EmptyClient(_FakeClient):
        def list_sessions(self, sender_id):
            return []

    adapter = DaemonTuiAdapter(_EmptyClient(), sender_id="cli")
    text = adapter.list_sessions_text("cli")
    assert text == "No sessions found."


def test_list_sessions_text_marks_active() -> None:
    client = _FakeClient()
    adapter = DaemonTuiAdapter(client, sender_id="cli")
    # Set the active session
    adapter._active_session_id = "sess-1"
    text = adapter.list_sessions_text("cli")
    assert "sess-1 *" in text
    assert "sess-x" in text
    assert "/resume" in text


# ---------------------------------------------------------------------------
# DaemonApiClient tests
# ---------------------------------------------------------------------------


class TestDaemonApiClient:
    def _make_client(self, tmp_path) -> DaemonApiClient:
        return DaemonApiClient(socket_path=tmp_path / "test.sock", timeout=5.0)

    def test_health_calls_correct_endpoint(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"status": "ok"}'
        mock_resp.json.return_value = {"status": "ok"}

        with patch.object(client, "_get_client") as mock_gc:
            mock_gc.return_value.request.return_value = mock_resp
            result = client.health()

        assert result == {"status": "ok"}
        mock_gc.return_value.request.assert_called_once_with(
            "GET", "/health", json=None, params=None
        )

    def test_status_calls_correct_endpoint(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"uptime": 100}'
        mock_resp.json.return_value = {"uptime": 100}

        with patch.object(client, "_get_client") as mock_gc:
            mock_gc.return_value.request.return_value = mock_resp
            result = client.status()

        assert result == {"uptime": 100}

    def test_close_closes_underlying_client(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_httpx = MagicMock()
        mock_httpx.is_closed = False
        client._client = mock_httpx

        client.close()
        mock_httpx.close.assert_called_once()
        assert client._client is None

    def test_close_noop_when_already_closed(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        client._client = None
        client.close()  # should not raise

    def test_context_manager(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_httpx = MagicMock()
        mock_httpx.is_closed = False
        client._client = mock_httpx

        with client:
            pass

        mock_httpx.close.assert_called_once()

    def test_request_error_extracts_json_detail(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.json.return_value = {"detail": "validation error"}

        with (
            patch.object(client, "_get_client") as mock_gc,
            pytest.raises(RuntimeError, match="validation error"),
        ):
            mock_gc.return_value.request.return_value = mock_resp
            client.health()

    def test_request_error_falls_back_to_text(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.side_effect = Exception("not json")
        mock_resp.text = "Internal Server Error"

        with (
            patch.object(client, "_get_client") as mock_gc,
            pytest.raises(RuntimeError, match="Internal Server Error"),
        ):
            mock_gc.return_value.request.return_value = mock_resp
            client.health()

    def test_request_empty_response(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_resp.text = ""

        with patch.object(client, "_get_client") as mock_gc:
            mock_gc.return_value.request.return_value = mock_resp
            result = client._request("DELETE", "/v1/foo")

        assert result == {}

    def test_send_message_with_session_id(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"text": "hi"}'
        mock_resp.json.return_value = {"text": "hi"}

        with patch.object(client, "_get_client") as mock_gc:
            mock_gc.return_value.request.return_value = mock_resp
            result = client.send_message("cli", "hello", session_id="s1")

        call_kwargs = mock_gc.return_value.request.call_args
        assert call_kwargs.kwargs["json"]["session_id"] == "s1"

    def test_send_message_without_session_id(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"text": "hi"}'
        mock_resp.json.return_value = {"text": "hi"}

        with patch.object(client, "_get_client") as mock_gc:
            mock_gc.return_value.request.return_value = mock_resp
            client.send_message("cli", "hello")

        call_kwargs = mock_gc.return_value.request.call_args
        assert "session_id" not in call_kwargs.kwargs["json"]

    def test_list_sessions(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '[{"session_id": "s1"}]'
        mock_resp.json.return_value = [{"session_id": "s1"}]

        with patch.object(client, "_get_client") as mock_gc:
            mock_gc.return_value.request.return_value = mock_resp
            result = client.list_sessions("cli")

        assert result == [{"session_id": "s1"}]

    def test_new_session(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"session_id": "new-1"}'
        mock_resp.json.return_value = {"session_id": "new-1"}

        with patch.object(client, "_get_client") as mock_gc:
            mock_gc.return_value.request.return_value = mock_resp
            result = client.new_session("cli")

        assert result["session_id"] == "new-1"

    def test_get_history(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"messages": [{"role": "user", "content": "hi"}]}'
        mock_resp.json.return_value = {"messages": [{"role": "user", "content": "hi"}]}

        with patch.object(client, "_get_client") as mock_gc:
            mock_gc.return_value.request.return_value = mock_resp
            result = client.get_history("cli", "s1")

        assert len(result) == 1
        assert result[0]["role"] == "user"
