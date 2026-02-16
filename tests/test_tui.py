"""Tests for the Textual TUI chat interface."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.session import Session, SessionManager
from taskrunner.tui import SENDER_ID, ChatApp, ChatInput, StatusBar


def _make_mock_server(tmp_path, handle_response="Mock response"):
    """Create a mock ChatServer with a real SessionManager."""
    mgr = SessionManager(sessions_dir=str(tmp_path))
    server = MagicMock()
    server._session_mgr = mgr
    server.handle_message = MagicMock(return_value=handle_response)
    server.get_or_create_session = mgr.get_or_create
    server.new_session = mgr.new_session
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
        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("What's the weather?")
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
        server.handle_message.assert_called_once()
        call_args = server.handle_message.call_args
        assert call_args[0] == (SENDER_ID, "What's the weather?")
        assert "on_text_delta" in call_args[1]


@pytest.mark.asyncio
async def test_input_disabled_during_processing(tmp_path):
    """Input should be disabled while the agent is processing."""
    event = asyncio.Event()

    def slow_handle(sender_id, text, **kwargs):
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
        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("Hello")
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
        # ChatInput starts empty, just press enter
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


@pytest.mark.asyncio
async def test_help_command(tmp_path):
    """/help should show command list without calling the server."""
    server = _make_mock_server(tmp_path)
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("/help")
        await pilot.press("enter")
        await pilot.pause()

        log = app.query_one("#chat-log")
        lines_text = "\n".join(str(line) for line in log.lines)
        assert "/compact" in lines_text
        assert "/exit" in lines_text
        assert "/new" in lines_text
        server.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_compact_command(tmp_path):
    """/compact should clear the display but not the session."""
    server = _make_mock_server(tmp_path)
    mgr = server._session_mgr
    mgr.add_user_message(SENDER_ID, "Hello there")

    app = ChatApp(server)

    async with app.run_test() as pilot:
        await pilot.pause()

        log = app.query_one("#chat-log")
        lines_text = "\n".join(str(line) for line in log.lines)
        assert "Hello there" in lines_text

        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("/compact")
        await pilot.press("enter")
        await pilot.pause()

        log = app.query_one("#chat-log")
        lines_text = "\n".join(str(line) for line in log.lines)
        assert "Hello there" not in lines_text
        assert "Display cleared" in lines_text

        # Session history should still be intact
        session = mgr.get_or_create(SENDER_ID)
        assert len(session.messages) == 1


@pytest.mark.asyncio
async def test_server_command_no_thinking(tmp_path):
    """Server commands like /sessions should not show Thinking indicator."""
    server = _make_mock_server(tmp_path, "Sessions:\n  ...")
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("/sessions")
        await pilot.press("enter")
        await pilot.pause()

        # Input should NOT be disabled (no worker launched)
        assert inp.disabled is False
        server.handle_message.assert_called_once_with(SENDER_ID, "/sessions")


@pytest.mark.asyncio
async def test_tui_supports_backend_session_methods(tmp_path):
    """ChatApp should use get_or_create_session/new_session if backend exposes them."""
    class _Backend:
        def __init__(self) -> None:
            self.new_calls = 0

        def handle_message(self, sender_id, text):
            return "ok"

        def get_or_create_session(self, sender_id):
            return SimpleNamespace(
                session_id="abc123",
                title="Remote",
                messages=[{"role": "user", "content": "hi"}],
            )

        def new_session(self, sender_id):
            self.new_calls += 1
            return SimpleNamespace(
                session_id="def456",
                title="",
                messages=[],
            )

    backend = _Backend()
    app = ChatApp(backend)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert "abc123" in app.sub_title

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert backend.new_calls == 1


@pytest.mark.asyncio
async def test_tui_prefers_backend_stream_events(tmp_path):
    """If backend provides stream_message, TUI should consume stream events."""
    class _StreamingBackend:
        def handle_message(self, sender_id, text, on_text_delta=None):
            raise AssertionError("TUI should use stream_message instead of handle_message")

        def stream_message(self, sender_id, text):
            del sender_id, text
            yield {"type": "start", "payload": {}}
            yield {"type": "token", "payload": {"text": "streamed "}}
            yield {"type": "token", "payload": {"text": "response"}}
            yield {"type": "final", "payload": {"text": "streamed response"}}

        def get_or_create_session(self, sender_id):
            return SimpleNamespace(session_id="abc123", title="Remote", messages=[])

        def new_session(self, sender_id):
            return SimpleNamespace(session_id="def456", title="", messages=[])

    app = ChatApp(_StreamingBackend())

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("hello")
        await pilot.press("enter")

        for _ in range(30):
            await pilot.pause()
            if not inp.disabled:
                break

        log = app.query_one("#chat-log")
        lines_text = "\n".join(str(line) for line in log.lines)
        assert "streamed response" in lines_text

# --- New tests for TUI polish ---


@pytest.mark.asyncio
async def test_status_bar_shows_model(tmp_path):
    """StatusBar should display the model name passed to ChatApp."""
    server = _make_mock_server(tmp_path)
    app = ChatApp(server, model_name="claude-sonnet-4-20250514")

    async with app.run_test() as pilot:
        await pilot.pause()

        bar = app.query_one("#status-bar", StatusBar)
        assert bar.model_name == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_status_bar_thinking_state(tmp_path):
    """StatusBar.is_thinking should be True during LLM call, False after."""
    event = asyncio.Event()

    def slow_handle(sender_id, text, **kwargs):
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
        inp = app.query_one("#chat-input", ChatInput)
        bar = app.query_one("#status-bar", StatusBar)

        inp.load_text("Hello")
        await pilot.press("enter")

        # Wait for thinking state to activate
        for _ in range(20):
            await pilot.pause()
            if bar.is_thinking:
                break
        assert bar.is_thinking is True

        # Release
        event.set()

        for _ in range(20):
            await pilot.pause()
            if not bar.is_thinking:
                break
        assert bar.is_thinking is False


@pytest.mark.asyncio
async def test_input_history(tmp_path):
    """Up arrow should recall previous messages in order."""
    server = _make_mock_server(tmp_path, "ok")
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input", ChatInput)

        # Send two messages — load text, then press enter to submit
        inp.load_text("first message")
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if not inp.disabled:
                break

        inp.load_text("second message")
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if not inp.disabled:
                break

        # Verify history was recorded
        assert len(inp._history) == 2
        assert inp._history[0] == "first message"
        assert inp._history[1] == "second message"

        # Up arrow should recall most recent first
        inp.focus()
        await pilot.press("up")
        await pilot.pause()
        assert "second message" in inp.text

        await pilot.press("up")
        await pilot.pause()
        assert "first message" in inp.text

        # Down arrow should go forward
        await pilot.press("down")
        await pilot.pause()
        assert "second message" in inp.text


@pytest.mark.asyncio
async def test_multiline_input(tmp_path):
    """Shift+Enter should insert newline, Enter should submit full text."""
    server = _make_mock_server(tmp_path, "ok")
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input", ChatInput)
        inp.focus()
        await pilot.pause()

        # Load text with a newline in the middle (simulating multi-line editing)
        inp.load_text("line one\nline two")
        await pilot.pause()
        assert "\n" in inp.text

        # Submit with Enter — the text includes the newline
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if not inp.disabled:
                break

        server.handle_message.assert_called_once()
        call_text = server.handle_message.call_args[0][1]
        assert "line one" in call_text
        assert "line two" in call_text


@pytest.mark.asyncio
async def test_status_command(tmp_path):
    """/status should route to server and display status info."""
    server = _make_mock_server(tmp_path, "Status:\n  Model: claude-sonnet-4-20250514\n  Session ID: abc123")
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("/status")
        await pilot.press("enter")
        await pilot.pause()

        assert inp.disabled is False
        server.handle_message.assert_called_once_with(SENDER_ID, "/status")


@pytest.mark.asyncio
async def test_model_command(tmp_path):
    """/model should route to server and display model info."""
    server = _make_mock_server(tmp_path, "Model:\n  Name: claude-sonnet-4-20250514")
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("/model")
        await pilot.press("enter")
        await pilot.pause()

        assert inp.disabled is False
        server.handle_message.assert_called_once_with(SENDER_ID, "/model")


@pytest.mark.asyncio
async def test_help_includes_new_commands(tmp_path):
    """/help should mention /status and /model."""
    server = _make_mock_server(tmp_path)
    app = ChatApp(server)

    async with app.run_test() as pilot:
        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("/help")
        await pilot.press("enter")
        await pilot.pause()

        log = app.query_one("#chat-log")
        lines_text = "\n".join(str(line) for line in log.lines)
        assert "/status" in lines_text
        assert "/model" in lines_text


@pytest.mark.asyncio
async def test_message_count(tmp_path):
    """StatusBar message count should increment on send and receive."""
    server = _make_mock_server(tmp_path, "response")
    app = ChatApp(server)

    async with app.run_test() as pilot:
        bar = app.query_one("#status-bar", StatusBar)
        assert bar.message_count == 0

        inp = app.query_one("#chat-input", ChatInput)
        inp.load_text("hello")
        await pilot.press("enter")

        for _ in range(20):
            await pilot.pause()
            if not inp.disabled:
                break

        # 1 for user message + 1 for assistant response
        assert bar.message_count == 2
