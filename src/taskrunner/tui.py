"""Textual TUI for interactive chat with the agent."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Footer, Header, RichLog, Static, TextArea

if TYPE_CHECKING:
    from taskrunner.chat import ChatServer

SENDER_ID = "cli"

_LOG_POLL_INTERVAL = 0.25

# Commands handled locally in the TUI (not sent to backend)
_TUI_COMMANDS = {"/compact", "/exit", "/quit", "/help"}
# Commands handled by backend that return instantly (no LLM call)
_SERVER_COMMANDS = {"/clear", "/reset", "/new", "/sessions"}
# /resume is a prefix match, handled separately

_HELP_TEXT = """\
[bold]Commands:[/bold]
  [cyan]/help[/cyan]         Show this help
  [cyan]/compact[/cyan]      Clear the display (keeps session history)
  [cyan]/new[/cyan]          Start a new session
  [cyan]/sessions[/cyan]     List all sessions
  [cyan]/resume <id>[/cyan]  Resume a session by ID
  [cyan]/clear[/cyan]        Clear session history
  [cyan]/exit[/cyan]         Quit

[bold]Shortcuts:[/bold]
  [cyan]ctrl+n[/cyan]        New session
  [cyan]ctrl+c[/cyan]        Quit
  [cyan]enter[/cyan]         Send message
  [cyan]shift+enter[/cyan]   New line

[bold]Tips:[/bold]
  Hold [cyan]shift[/cyan] and drag to select/copy text
  Use [cyan]↑[/cyan]/[cyan]↓[/cyan] to recall previous messages\
"""


class _QueueLogHandler(logging.Handler):
    """Pushes log records into a thread-safe queue for the TUI to poll."""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self._queue = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


class ConfirmBar(Widget):
    """Inline widget with Yes/No buttons for guardian review confirmations."""

    def __init__(
        self,
        tool_name: str,
        reason: str,
        event: threading.Event,
        result: dict,
    ) -> None:
        super().__init__()
        self._event = event
        self._result = result
        self._tool_name = tool_name
        self._reason = reason

    def compose(self) -> ComposeResult:
        yield Static(
            f"  Allow [bold]{self._tool_name}[/bold]? ({self._reason})",
            markup=True,
        )
        with Horizontal(id="confirm-buttons"):
            yield Button("Yes", id="confirm-yes", variant="success")
            yield Button("No", id="confirm-no", variant="error")

    def on_mount(self) -> None:
        self.query_one("#confirm-yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._result["allowed"] = event.button.id == "confirm-yes"
        self._event.set()
        self.remove()


class StatusBar(Static):
    """Always-visible status bar showing model, thinking state, and message count."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 2;
    }
    """

    model_name: reactive[str] = reactive("", init=False)
    is_thinking: reactive[bool] = reactive(False, init=False)
    message_count: reactive[int] = reactive(0, init=False)

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._think_start: float = 0.0
        self._refresh_timer = None

    def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(0.5, self._tick)
        self._render_bar()

    def _tick(self) -> None:
        if self.is_thinking:
            self._render_bar()

    def start_thinking(self) -> None:
        self._think_start = time.monotonic()
        self.is_thinking = True
        self._render_bar()

    def stop_thinking(self) -> None:
        self.is_thinking = False
        self._render_bar()

    def watch_model_name(self) -> None:
        self._render_bar()

    def watch_message_count(self) -> None:
        self._render_bar()

    def _render_bar(self) -> None:
        parts: list[str] = []
        if self.model_name:
            parts.append(self.model_name)

        if self.is_thinking:
            elapsed = int(time.monotonic() - self._think_start)
            parts.append(f"● Thinking... ({elapsed}s)")
        else:
            parts.append("● Ready")

        parts.append(f"{self.message_count} msgs")
        self.update("  ".join(parts))


class ChatInput(TextArea):
    """Multi-line input with Enter-to-submit and input history."""

    DEFAULT_CSS = """
    ChatInput {
        dock: bottom;
        height: auto;
        min-height: 3;
        max-height: 8;
        margin: 1 2;
        border: tall $accent;
    }
    """

    class Submitted(Message):
        """Posted when user submits input."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs) -> None:
        super().__init__(
            show_line_numbers=False,
            soft_wrap=True,
            **kwargs,
        )
        self._history: list[str] = []
        self._history_index: int = -1
        self._draft: str = ""

    def _on_key(self, event) -> None:
        """Handle enter, shift+enter, and arrow keys."""
        if event.key == "enter":
            text = self.text.strip()
            if text:
                self._history.append(text)
                self._history_index = -1
                self._draft = ""
                self.post_message(self.Submitted(text))
                self.clear()
            event.prevent_default()
            event.stop()
        elif event.key == "shift+enter":
            self.insert("\n")
            event.prevent_default()
            event.stop()
        elif event.key == "up" and self.cursor_location[0] == 0:
            self._navigate_history(1)
            event.prevent_default()
            event.stop()
        elif event.key == "down" and self.cursor_location[0] == self.document.line_count - 1:
            self._navigate_history(-1)
            event.prevent_default()
            event.stop()

    def _navigate_history(self, direction: int) -> None:
        if not self._history:
            return

        if self._history_index == -1:
            self._draft = self.text

        new_index = self._history_index + direction

        if new_index < -1:
            return
        if new_index >= len(self._history):
            return

        self._history_index = new_index

        if new_index == -1:
            self.load_text(self._draft)
        else:
            # History is stored oldest-first; navigate from newest
            idx = len(self._history) - 1 - new_index
            self.load_text(self._history[idx])


class ChatApp(App):
    """Textual TUI for agent chat."""

    TITLE = "Agent Chat"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+n", "new_session", "New Session"),
    ]

    CSS = """
    #chat-log {
        padding: 1 2;
    }
    ConfirmBar {
        dock: bottom;
        height: auto;
        max-height: 5;
        background: $warning 20%;
        padding: 1 2;
    }
    #confirm-buttons {
        height: 3;
        align: left middle;
    }
    #confirm-buttons Button {
        margin: 0 1;
        min-width: 10;
    }
    """

    def __init__(
        self,
        server: ChatServer | object,
        sender_id: str = SENDER_ID,
        model_name: str = "",
    ) -> None:
        super().__init__()
        self._server = server
        self._sender_id = sender_id
        self._model_name = model_name
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._log_handler: _QueueLogHandler | None = None
        self._log_poller = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="chat-log", markup=True, wrap=True, auto_scroll=True)
        yield StatusBar(id="status-bar")
        yield ChatInput(id="chat-input")
        yield Footer()

    def on_mount(self) -> None:
        bar = self.query_one("#status-bar", StatusBar)
        bar.model_name = self._model_name
        self.query_one("#chat-input", ChatInput).focus()
        self._update_subtitle()
        self._replay_history()
        self._install_log_handler()

    def _install_log_handler(self) -> None:
        """Route Python logging into a queue, polled by a timer."""
        self._log_handler = _QueueLogHandler(self._log_queue)
        self._log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logging.getLogger("taskrunner").addHandler(self._log_handler)
        self._log_poller = self.set_interval(_LOG_POLL_INTERVAL, self._poll_logs)

    def _poll_logs(self) -> None:
        """Drain the log queue and show the latest message as a toast."""
        latest = None
        while True:
            try:
                latest = self._log_queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self.notify(latest, timeout=4)

    @on(ChatInput.Submitted)
    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text.strip()
        if not text:
            return

        cmd = text.split()[0].lower()

        # TUI-local commands
        if cmd in _TUI_COMMANDS:
            self._handle_tui_command(cmd)
            return

        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold cyan]You:[/bold cyan] {text}")

        bar = self.query_one("#status-bar", StatusBar)
        bar.message_count += 1

        # Server commands that return instantly (no LLM call)
        if cmd in _SERVER_COMMANDS or cmd == "/resume":
            response = self._server.handle_message(self._sender_id, text)
            self._append_response(response)
            self._update_subtitle()
            return

        # Normal message — send to agent
        inp = self.query_one("#chat-input", ChatInput)
        inp.disabled = True
        self._send_message(text)

    def _handle_tui_command(self, cmd: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        if cmd == "/help":
            log.write(_HELP_TEXT)
        elif cmd == "/compact":
            log.clear()
            log.write("[dim]Display cleared. Session history preserved.[/dim]")
        elif cmd in ("/exit", "/quit"):
            self.action_quit()

    @work(thread=True)
    def _send_message(self, text: str) -> None:
        self.call_from_thread(self._show_status)
        try:
            response = self._server.handle_message(self._sender_id, text)
        except Exception as exc:
            response = f"Error: {exc}"

        self.call_from_thread(self._hide_status)
        self.call_from_thread(self._append_response, response)
        self.call_from_thread(self._enable_input)
        self.call_from_thread(self._update_subtitle)

    def _show_status(self) -> None:
        self.query_one("#status-bar", StatusBar).start_thinking()

    def _hide_status(self) -> None:
        self.query_one("#status-bar", StatusBar).stop_thinking()

    def _append_response(self, text: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(Text("Assistant:", style="bold green"))
        log.write(RichMarkdown(text))
        log.write("")
        bar = self.query_one("#status-bar", StatusBar)
        bar.message_count += 1

    def _enable_input(self) -> None:
        inp = self.query_one("#chat-input", ChatInput)
        inp.disabled = False
        inp.focus()

    def _replay_history(self) -> None:
        """Render recent messages from the active session into the RichLog."""
        session = self._get_or_create_session()
        log = self.query_one("#chat-log", RichLog)
        bar = self.query_one("#status-bar", StatusBar)

        count = 0
        messages = (session.messages or [])[-20:]
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str):
                log.write(f"[bold cyan]You:[/bold cyan] {content}")
                count += 1
            elif role == "assistant":
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                else:
                    continue
                if text:
                    log.write(Text("Assistant:", style="bold green"))
                    log.write(RichMarkdown(text))
                    log.write("")
                    count += 1

        bar.message_count = count

    def _update_subtitle(self) -> None:
        session = self._get_or_create_session()
        title = session.title or "(new session)"
        self.sub_title = f"Session {session.session_id}: {title}"

    def action_new_session(self) -> None:
        session = self._new_session()
        log = self.query_one("#chat-log", RichLog)
        log.clear()
        log.write(f"[dim]Started new session {session.session_id}.[/dim]")
        bar = self.query_one("#status-bar", StatusBar)
        bar.message_count = 0
        self._update_subtitle()

    def _get_or_create_session(self):
        if hasattr(self._server, "_session_mgr"):
            return self._server._session_mgr.get_or_create(self._sender_id)
        get_session = getattr(self._server, "get_or_create_session", None)
        if callable(get_session):
            return get_session(self._sender_id)
        raise RuntimeError("TUI backend does not expose session access methods")

    def _new_session(self):
        if hasattr(self._server, "_session_mgr"):
            return self._server._session_mgr.new_session(self._sender_id)
        new_session = getattr(self._server, "new_session", None)
        if callable(new_session):
            return new_session(self._sender_id)
        raise RuntimeError("TUI backend does not expose new_session()")

    def action_quit(self) -> None:
        if self._log_handler:
            logging.getLogger("taskrunner").removeHandler(self._log_handler)
        self.exit()


def _make_tui_confirm_fn(app: ChatApp):
    """Create a confirm_fn that bridges a worker thread to the TUI.

    Returns a callable suitable for ChatServer(confirm_fn=...).
    """

    def confirm(tool_name: str, tool_input: dict, reason: str) -> bool:
        event = threading.Event()
        result: dict = {"allowed": False}

        def _mount_bar() -> None:
            log = app.query_one("#chat-log", RichLog)
            subject = tool_input.get("subject")
            if subject:
                header = (
                    f"[bold yellow]⚠ Guardian review:[/bold yellow] "
                    f'[bold]{tool_name}[/bold] — "{subject}"\n'
                    f"  Input: {tool_input}"
                )
            else:
                header = (
                    f"[bold yellow]⚠ Guardian review:[/bold yellow] "
                    f"[bold]{tool_name}[/bold]({tool_input})"
                )
            log.write(f"{header}\n  Reason: {reason}")
            bar = ConfirmBar(tool_name, reason, event, result)
            app.mount(bar, before="#chat-input")

        app.call_from_thread(_mount_bar)
        event.wait(timeout=300)
        return result["allowed"]

    return confirm
