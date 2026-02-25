"""Tests for `creel send` command behaviors."""

from __future__ import annotations

import argparse
from pathlib import Path

from creel import cli


def _make_args(tmp_path: Path, stream: bool = True, auto_approve: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        sender_id="cli",
        message="hello",
        session_id=None,
        socket_path=tmp_path / "daemon.sock",
        timeout=5.0,
        stream=stream,
        auto_approve=auto_approve,
    )


def test_cmd_send_stream_prints_tokens(tmp_path: Path, monkeypatch, capsys) -> None:
    class _FakeClient:
        def __init__(self, socket_path, timeout):
            del socket_path, timeout

        def stream_message(self, sender_id: str, text: str, session_id=None, auto_approve=False):
            del sender_id, text, session_id, auto_approve
            yield {"type": "start", "payload": {}}
            yield {"type": "token", "payload": {"text": "echo:"}}
            yield {"type": "token", "payload": {"text": "hello"}}
            yield {"type": "final", "payload": {"text": "echo:hello"}}

    monkeypatch.setattr("creel.daemon.client.DaemonApiClient", _FakeClient)
    args = _make_args(tmp_path, stream=True)

    rc = cli.cmd_send(args)
    out = capsys.readouterr()

    assert rc == 0
    assert out.out.strip() == "echo:hello"


def test_cmd_send_stream_error_event(tmp_path: Path, monkeypatch, capsys) -> None:
    class _FakeClient:
        def __init__(self, socket_path, timeout):
            del socket_path, timeout

        def stream_message(self, sender_id: str, text: str, session_id=None, auto_approve=False):
            del sender_id, text, session_id, auto_approve
            yield {"type": "error", "payload": {"error": "boom"}}

    monkeypatch.setattr("creel.daemon.client.DaemonApiClient", _FakeClient)
    args = _make_args(tmp_path, stream=True)

    rc = cli.cmd_send(args)
    out = capsys.readouterr()

    assert rc == 1
    assert "boom" in out.err


def test_cmd_send_stream_auto_approve_forwarded(tmp_path: Path, monkeypatch, capsys) -> None:
    """--auto-approve flag is forwarded to stream_message."""
    received_auto_approve = []

    class _FakeClient:
        def __init__(self, socket_path, timeout):
            del socket_path, timeout

        def stream_message(self, sender_id: str, text: str, session_id=None, auto_approve=False):
            received_auto_approve.append(auto_approve)
            yield {"type": "final", "payload": {"text": "done"}}

    monkeypatch.setattr("creel.daemon.client.DaemonApiClient", _FakeClient)
    args = _make_args(tmp_path, stream=True, auto_approve=True)

    rc = cli.cmd_send(args)

    assert rc == 0
    assert received_auto_approve == [True]


def test_cmd_send_non_stream_auto_approve_in_payload(tmp_path: Path, monkeypatch, capsys) -> None:
    """--auto-approve flag is included in non-streaming HTTP payload."""
    import json

    captured_payloads = []

    class _FakeResponse:
        status_code = 200
        def json(self):
            return {"text": "done"}

    def _fake_request(socket_path, method, path, json_body=None, timeout=None):
        captured_payloads.append(json_body)
        return _FakeResponse()

    monkeypatch.setattr(cli, "_daemon_request", _fake_request)
    args = _make_args(tmp_path, stream=False, auto_approve=True)

    rc = cli.cmd_send(args)

    assert rc == 0
    assert captured_payloads[0].get("auto_approve") is True
