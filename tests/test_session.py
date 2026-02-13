"""Tests for the session manager."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskrunner.session import Session, SessionManager, _sanitize_sender_id


# -- existing tests (updated for new session_id / title fields) --


def test_create_new_session(tmp_path: Path) -> None:
    """get_or_create should create a new session when none exists."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.get_or_create("+12345678901")

    assert session.sender_id == "+12345678901"
    assert session.messages == []
    assert len(session.session_id) == 8  # hex(4) = 8 chars
    assert session.title == ""


def test_add_user_message(tmp_path: Path) -> None:
    """add_user_message should append and persist."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.add_user_message("+12345678901", "Hello")

    assert len(session.messages) == 1
    assert session.messages[0] == {"role": "user", "content": "Hello"}

    # Verify persisted to disk
    files = [f for f in tmp_path.glob("*.json") if f.name != "_active.json"]
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert len(data["messages"]) == 1
    assert data["session_id"] == session.session_id


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
    """clear should reset the active session's messages but keep the file."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.add_user_message("cli", "Hi")

    session_files = [f for f in tmp_path.glob("*.json") if f.name != "_active.json"]
    assert len(session_files) == 1

    mgr.clear("cli")

    # File still exists but messages are empty
    session_files = [f for f in tmp_path.glob("*.json") if f.name != "_active.json"]
    assert len(session_files) == 1
    data = json.loads(session_files[0].read_text())
    assert data["messages"] == []

    # get_or_create should return same session with empty messages
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

    # Create a session so we know its ID, then corrupt its file
    session = mgr.get_or_create("cli")
    sid = session.session_id
    mgr._save(session)
    (tmp_path / f"{sid}.json").write_text("not valid json{{{")

    # get_or_create should return a fresh session (can't load the corrupt one)
    new_session = mgr.get_or_create("cli")
    assert new_session.messages == []


# -- new multi-session tests --


def test_new_session_creates_fresh(tmp_path: Path) -> None:
    """new_session should create a fresh session and archive the old one."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    s1 = mgr.add_user_message("cli", "First conversation")
    s1_id = s1.session_id

    s2 = mgr.new_session("cli")
    assert s2.session_id != s1_id
    assert s2.messages == []
    assert s2.sender_id == "cli"

    # Old session file still exists
    assert (tmp_path / f"{s1_id}.json").exists()
    # New session is now active
    assert mgr._get_active_session_id("cli") == s2.session_id


def test_list_sessions(tmp_path: Path) -> None:
    """list_sessions should return metadata sorted by last_active desc."""
    mgr = SessionManager(sessions_dir=str(tmp_path))

    # Create two sessions
    s1 = mgr.add_user_message("cli", "Session one")
    s1_id = s1.session_id

    s2 = mgr.new_session("cli")
    mgr.add_user_message("cli", "Session two")
    s2_id = s2.session_id

    sessions = mgr.list_sessions("cli")
    assert len(sessions) == 2
    # Most recent first
    assert sessions[0]["session_id"] == s2_id
    assert sessions[1]["session_id"] == s1_id
    assert sessions[0]["title"] == "Session two"
    assert sessions[1]["title"] == "Session one"
    assert sessions[0]["message_count"] == 1
    assert sessions[1]["message_count"] == 1


def test_list_sessions_filters_by_sender(tmp_path: Path) -> None:
    """list_sessions should only return sessions for the given sender."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.add_user_message("cli", "CLI session")
    mgr.add_user_message("phone", "Phone session")

    cli_sessions = mgr.list_sessions("cli")
    assert len(cli_sessions) == 1
    assert cli_sessions[0]["title"] == "CLI session"

    phone_sessions = mgr.list_sessions("phone")
    assert len(phone_sessions) == 1
    assert phone_sessions[0]["title"] == "Phone session"


def test_resume_session(tmp_path: Path) -> None:
    """resume_session should switch the active session."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    s1 = mgr.add_user_message("cli", "First")
    s1_id = s1.session_id

    s2 = mgr.new_session("cli")
    s2_id = s2.session_id
    assert mgr._get_active_session_id("cli") == s2_id

    resumed = mgr.resume_session("cli", s1_id)
    assert resumed.session_id == s1_id
    assert resumed.messages[0]["content"] == "First"
    assert mgr._get_active_session_id("cli") == s1_id


def test_resume_session_bad_id(tmp_path: Path) -> None:
    """resume_session with a nonexistent ID should raise ValueError."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.add_user_message("cli", "Hello")

    with pytest.raises(ValueError, match="not found"):
        mgr.resume_session("cli", "nonexistent")


def test_resume_session_wrong_sender(tmp_path: Path) -> None:
    """resume_session should reject sessions belonging to another sender."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    s1 = mgr.add_user_message("cli", "CLI session")

    with pytest.raises(ValueError, match="not found"):
        mgr.resume_session("phone", s1.session_id)


def test_active_index_persistence(tmp_path: Path) -> None:
    """Active index should persist across SessionManager instances."""
    mgr1 = SessionManager(sessions_dir=str(tmp_path))
    s1 = mgr1.add_user_message("cli", "Hello")
    s1_id = s1.session_id

    # New manager instance should find the same active session
    mgr2 = SessionManager(sessions_dir=str(tmp_path))
    s2 = mgr2.get_or_create("cli")
    assert s2.session_id == s1_id
    assert s2.messages[0]["content"] == "Hello"


def test_title_set_from_first_message(tmp_path: Path) -> None:
    """Title should be set from the first user message only."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    s = mgr.add_user_message("cli", "What is the weather today?")
    assert s.title == "What is the weather today?"

    # Second message should not change the title
    s = mgr.add_user_message("cli", "Something else entirely")
    assert s.title == "What is the weather today?"


def test_title_truncated_at_60_chars(tmp_path: Path) -> None:
    """Title should be truncated to 60 characters."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    long_msg = "A" * 100
    s = mgr.add_user_message("cli", long_msg)
    assert len(s.title) == 60


def test_backward_compat_old_session_file(tmp_path: Path) -> None:
    """Old session files without session_id/title should load gracefully."""
    # Write an old-format session file
    old_data = {
        "sender_id": "cli",
        "created_at": 1000000.0,
        "last_active": 1000001.0,
        "messages": [{"role": "user", "content": "Old message"}],
    }
    session_id = "oldfile01"
    (tmp_path / f"{session_id}.json").write_text(json.dumps(old_data))

    # Point the active index at it
    (tmp_path / "_active.json").write_text(json.dumps({"cli": session_id}))

    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.get_or_create("cli")

    assert session.sender_id == "cli"
    assert session.session_id == session_id
    assert session.title == ""
    assert len(session.messages) == 1
    assert session.messages[0]["content"] == "Old message"
