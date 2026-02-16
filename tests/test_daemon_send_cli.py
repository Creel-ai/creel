"""Tests for `creel send` command behaviors."""

from __future__ import annotations

import argparse
from pathlib import Path

from taskrunner import cli


def _make_args(tmp_path: Path, stream: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        sender_id="cli",
        message="hello",
        session_id=None,
        socket_path=tmp_path / "daemon.sock",
        timeout=5.0,
        stream=stream,
    )


def test_cmd_send_stream_prints_tokens(tmp_path: Path, monkeypatch, capsys) -> None:
    class _FakeClient:
        def __init__(self, socket_path, timeout):
            del socket_path, timeout

        def stream_message(self, sender_id: str, text: str, session_id=None):
            del sender_id, text, session_id
            yield {"type": "start", "payload": {}}
            yield {"type": "token", "payload": {"text": "echo:"}}
            yield {"type": "token", "payload": {"text": "hello"}}
            yield {"type": "final", "payload": {"text": "echo:hello"}}

    monkeypatch.setattr("taskrunner.daemon.client.DaemonApiClient", _FakeClient)
    args = _make_args(tmp_path, stream=True)

    rc = cli.cmd_send(args)
    out = capsys.readouterr()

    assert rc == 0
    assert out.out.strip() == "echo:hello"


def test_cmd_send_stream_error_event(tmp_path: Path, monkeypatch, capsys) -> None:
    class _FakeClient:
        def __init__(self, socket_path, timeout):
            del socket_path, timeout

        def stream_message(self, sender_id: str, text: str, session_id=None):
            del sender_id, text, session_id
            yield {"type": "error", "payload": {"error": "boom"}}

    monkeypatch.setattr("taskrunner.daemon.client.DaemonApiClient", _FakeClient)
    args = _make_args(tmp_path, stream=True)

    rc = cli.cmd_send(args)
    out = capsys.readouterr()

    assert rc == 1
    assert "boom" in out.err
