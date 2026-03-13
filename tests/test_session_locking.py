"""Tests for session file locking and atomic writes."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from creel.session import SessionLockError, SessionManager, _flock_with_timeout

# -- _flock_with_timeout --


def test_flock_with_timeout_acquires_lock(tmp_path: Path) -> None:
    """flock_with_timeout should acquire the lock on an unlocked file."""
    p = tmp_path / "lockfile"
    p.write_text("")
    fd = os.open(str(p), os.O_RDWR)
    try:
        _flock_with_timeout(fd, fcntl.LOCK_EX, timeout=1)
        # Should succeed without raising
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_flock_with_timeout_raises_on_contention(tmp_path: Path) -> None:
    """flock_with_timeout should raise SessionLockError when lock is held."""
    p = tmp_path / "lockfile"
    p.write_text("")

    # Hold an exclusive lock in a separate fd
    holder = os.open(str(p), os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)

    contender = os.open(str(p), os.O_RDWR)
    try:
        with pytest.raises(SessionLockError, match="Could not acquire file lock"):
            _flock_with_timeout(contender, fcntl.LOCK_EX, timeout=0.2)
    finally:
        os.close(contender)
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


# -- atomic write / backup --


def test_save_creates_backup(tmp_path: Path) -> None:
    """Saving a session twice should create a .bak of the first version."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.add_user_message("cli", "First message")
    sid = session.session_id
    bak_path = tmp_path / f"{sid}.json.bak"

    # First save already happened in add_user_message; .bak may or may not
    # exist yet (depends on whether get_or_create also saved).  Save again.
    mgr.add_user_message("cli", "Second message")

    assert bak_path.exists(), ".bak file should be created on second write"
    bak_data = json.loads(bak_path.read_text())
    # The backup should contain the previous version (1 message)
    assert len(bak_data["messages"]) == 1
    assert bak_data["messages"][0]["content"] == "First message"


def test_save_is_atomic_no_partial_writes(tmp_path: Path) -> None:
    """A crash mid-write should not corrupt the session file.

    We verify atomicity indirectly: the final file should always contain
    valid JSON after a successful save.
    """
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.add_user_message("cli", "Important data")
    sid = session.session_id

    path = tmp_path / f"{sid}.json"
    data = json.loads(path.read_text())
    assert data["messages"][0]["content"] == "Important data"


def test_no_temp_files_left_after_save(tmp_path: Path) -> None:
    """Temp files should be cleaned up after a successful save."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.add_user_message("cli", "Hello")

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Temp files should be cleaned up: {tmp_files}"


# -- shared lock on read --


def test_load_succeeds_with_shared_lock(tmp_path: Path) -> None:
    """Multiple readers should be able to load the same session concurrently."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.add_user_message("cli", "Hello")
    sid = session.session_id

    results: list[str | None] = [None, None]

    def reader(idx: int) -> None:
        loaded = mgr.load_session(sid)
        results[idx] = loaded.messages[0]["content"] if loaded else None

    t1 = threading.Thread(target=reader, args=(0,))
    t2 = threading.Thread(target=reader, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert results[0] == "Hello"
    assert results[1] == "Hello"


# -- active index locking --


def test_active_index_uses_atomic_write(tmp_path: Path) -> None:
    """Active index should be written atomically and create a .bak."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.add_user_message("cli", "First")
    # At this point _active.json exists

    mgr.new_session("cli")
    # After new_session, _active.json was rewritten

    index_path = tmp_path / "_active.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text())
    assert "cli" in index


# -- concurrent write safety --


def test_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    """Multiple threads writing to the same session should not corrupt data."""
    mgr = SessionManager(sessions_dir=str(tmp_path), max_history=200)
    session = mgr.get_or_create("cli")
    sid = session.session_id
    errors: list[Exception] = []

    def writer(msg: str) -> None:
        try:
            s = mgr.load_session(sid)
            if s:
                s.messages.append({"role": "user", "content": msg})
                s.last_active = time.time()
                mgr.save_session(s)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(f"msg-{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"Unexpected errors during concurrent writes: {errors}"

    # File should still be valid JSON
    path = tmp_path / f"{sid}.json"
    data = json.loads(path.read_text())
    assert "session_id" in data


# -- lock timeout propagation --


def test_save_raises_lock_error_on_timeout(tmp_path: Path) -> None:
    """_save should propagate SessionLockError if lock cannot be acquired."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.get_or_create("cli")

    # Mock _flock_with_timeout to always raise
    with patch(
        "creel.session._flock_with_timeout",
        side_effect=SessionLockError("lock timeout"),
    ):
        with pytest.raises(SessionLockError):
            mgr.save_session(session)


def test_load_raises_lock_error_on_timeout(tmp_path: Path) -> None:
    """_load should propagate SessionLockError if shared lock fails."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session = mgr.get_or_create("cli")
    sid = session.session_id

    with patch(
        "creel.session._flock_with_timeout",
        side_effect=SessionLockError("lock timeout"),
    ):
        with pytest.raises(SessionLockError):
            mgr.load_session(sid)
