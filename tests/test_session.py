"""Tests for the session manager."""

from __future__ import annotations

import json
from pathlib import Path

from taskrunner.session import Session, SessionManager, _sanitize_sender_id


def test_create_new_session(tmp_path: Path) -> None:
    """get_or_create should create a new session when none exists."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.get_or_create("+12345678901")

    assert session.sender_id == "+12345678901"
    assert session.messages == []


def test_add_user_message(tmp_path: Path) -> None:
    """add_user_message should append and persist."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.add_user_message("+12345678901", "Hello")

    assert len(session.messages) == 1
    assert session.messages[0] == {"role": "user", "content": "Hello"}

    # Verify persisted to disk
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert len(data["messages"]) == 1


def test_add_assistant_response(tmp_path: Path) -> None:
    """add_assistant_response should append assistant content."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.add_user_message("cli", "Hi")
    mgr.add_assistant_response("cli", [{"type": "text", "text": "Hello!"}])

    session = mgr.get_or_create("cli")
    assert len(session.messages) == 2
    assert session.messages[1]["role"] == "assistant"


def test_add_tool_results(tmp_path: Path) -> None:
    """add_tool_results should append as user message."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.add_user_message("cli", "Check weather")
    mgr.add_tool_results("cli", [
        {"type": "tool_result", "tool_use_id": "t1", "content": "sunny"},
    ])

    session = mgr.get_or_create("cli")
    assert len(session.messages) == 2
    assert session.messages[1]["role"] == "user"
    assert session.messages[1]["content"][0]["type"] == "tool_result"


def test_clear_session(tmp_path: Path) -> None:
    """clear should delete the session file."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.add_user_message("cli", "Hi")

    assert list(tmp_path.glob("*.json"))  # File exists

    mgr.clear("cli")
    assert not list(tmp_path.glob("*.json"))  # File deleted

    # get_or_create should return fresh session
    session = mgr.get_or_create("cli")
    assert session.messages == []


def test_max_history_trimming(tmp_path: Path) -> None:
    """Messages beyond max_history should be trimmed."""
    mgr = SessionManager(sessions_dir=str(tmp_path), max_history=5)

    for i in range(10):
        mgr.add_user_message("cli", f"Message {i}")

    session = mgr.get_or_create("cli")
    assert len(session.messages) == 5
    # Should keep the most recent messages
    assert session.messages[0]["content"] == "Message 5"
    assert session.messages[4]["content"] == "Message 9"


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Session should survive creating a new SessionManager instance."""
    mgr1 = SessionManager(sessions_dir=str(tmp_path))
    mgr1.add_user_message("cli", "Hello")

    mgr2 = SessionManager(sessions_dir=str(tmp_path))
    session = mgr2.get_or_create("cli")
    assert len(session.messages) == 1
    assert session.messages[0]["content"] == "Hello"


def test_sanitize_sender_id_phone():
    """Phone numbers should be sanitized to digits."""
    assert _sanitize_sender_id("+1 (234) 567-8901") == "12345678901"


def test_sanitize_sender_id_alpha():
    """Alphanumeric IDs should stay as-is."""
    assert _sanitize_sender_id("cli") == "cli"
    assert _sanitize_sender_id("user_123") == "user_123"


def test_sanitize_sender_id_empty():
    """Empty input should return 'unknown'."""
    assert _sanitize_sender_id("") == "unknown"
    assert _sanitize_sender_id("!!!") == "unknown"


def test_corrupt_session_file(tmp_path: Path) -> None:
    """Corrupt session file should be handled gracefully."""
    mgr = SessionManager(sessions_dir=str(tmp_path))

    # Write garbage
    (tmp_path / "cli.json").write_text("not valid json{{{")

    session = mgr.get_or_create("cli")
    assert session.messages == []  # Fresh session
