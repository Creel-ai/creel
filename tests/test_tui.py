"""Tests for the Textual TUI chat interface."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.session import Session, SessionManager
from taskrunner.tui import SENDER_ID, ChatApp


def _make_mock_server(tmp_path, handle_response="Mock response"):
    """Create a mock ChatServer with a real SessionManager."""
    server = MagicMock()
    server._session_mgr = SessionManager(sessions_dir=str(tmp_path))
    server.handle_message = MagicMock(return_value=handle_response)
    return server


@pytest.fixture
def mock_server(tmp_path):
    return _make_mock_server(tmp_path)


@pytest.mark.asyncio
async def test_message_send(tmp_path):
    """Typing text + enter should show user message and mock response."""
    server = _make_mock_server(tmp_path, "It's sunny and 72°F.")
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input")
        inp.value = "What's the weather?"
        await pilot.press("enter")

        # Give the worker thread time to complete
        for _ in range(20):
            await pilot.pause()
            if not inp.disabled:
                break

        log = app.query_one("#chat-log")
        lines_text = "\n".join(str(line) for line in log.lines)
        assert "You:" in lines_text
        assert "weather" in lines_text
        assert "sunny" in lines_text or "Mock" in lines_text
        server.handle_message.assert_called_once_with(SENDER_ID, "What's the weather?")


@pytest.mark.asyncio
async def test_input_disabled_during_processing(tmp_path):
    """Input should be disabled while the agent is processing."""
    event = asyncio.Event()

    def slow_handle(sender_id, text):
        # Block until we release
        import time
        for _ in range(50):
            if event.is_set():
                break
            time.sleep(0.05)
        return "Done"

    server = _make_mock_server(tmp_path)
    server.handle_message = slow_handle
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input")
        inp.value = "Hello"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        # Input should be disabled while processing
        assert inp.disabled is True

        # Release the handler
        event.set()

        for _ in range(20):
            await pilot.pause()
            if not inp.disabled:
                break

        assert inp.disabled is False


@pytest.mark.asyncio
async def test_empty_input_ignored(tmp_path):
    """Submitting empty input should not send a message."""
    server = _make_mock_server(tmp_path)
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input")
        inp.value = ""
        await pilot.press("enter")
        await pilot.pause()

        server.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_new_session_keybinding(tmp_path):
    """Ctrl+N should start a new session."""
    server = _make_mock_server(tmp_path)
    app = ChatApp(server)

    async with app.run_test() as pilot:
        # Get initial session
        s1 = server._session_mgr.get_or_create(SENDER_ID)
        s1_id = s1.session_id

        await pilot.press("ctrl+n")
        await pilot.pause()

        # Active session should be different now
        s2 = server._session_mgr.get_or_create(SENDER_ID)
        assert s2.session_id != s1_id

        log = app.query_one("#chat-log")
        lines_text = "\n".join(str(line) for line in log.lines)
        assert "new session" in lines_text.lower()


@pytest.mark.asyncio
async def test_history_replay_on_mount(tmp_path):
    """On mount, recent messages from the active session should be displayed."""
    server = _make_mock_server(tmp_path)

    # Pre-populate a session with some history
    mgr = server._session_mgr
    mgr.add_user_message(SENDER_ID, "Hello there")
    mgr.add_assistant_response(SENDER_ID, [{"type": "text", "text": "Hi! How can I help?"}])
    mgr.add_user_message(SENDER_ID, "What time is it?")

    app = ChatApp(server)

    async with app.run_test() as pilot:
        await pilot.pause()

        log = app.query_one("#chat-log")
        lines_text = "\n".join(str(line) for line in log.lines)
        assert "Hello there" in lines_text
        assert "How can I help" in lines_text
        assert "What time is it" in lines_text


@pytest.mark.asyncio
async def test_subtitle_shows_session_info(tmp_path):
    """Subtitle should show session ID and title."""
    server = _make_mock_server(tmp_path)
    mgr = server._session_mgr
    mgr.add_user_message(SENDER_ID, "Weather question")

    app = ChatApp(server)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Session" in app.sub_title
        assert "Weather question" in app.sub_title
