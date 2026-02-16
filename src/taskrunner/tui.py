"""Textual TUI for interactive chat with the agent."""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

if TYPE_CHECKING:
    from taskrunner.chat import ChatServer

SENDER_ID = "cli"

_LOG_POLL_INTERVAL = 0.25

# Commands handled locally in the TUI (not sent to ChatServer)
_TUI_COMMANDS = {"/compact", "/exit", "/quit", "/help"}
# Commands handled by ChatServer that return instantly (no LLM call)
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

[bold]Tips:[/bold]
  Hold [cyan]shift[/cyan] and drag to select/copy text\
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
    #status {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 2;
        display: none;
    }
    #streaming-buffer {
        padding: 0 2;
    }
    #chat-input {
        dock: bottom;
        margin: 2 2;
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

    def __init__(self, server: ChatServer) -> None:
        super().__init__()
        self._server = server
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._log_handler: _QueueLogHandler | None = None
        self._log_poller = None
        self._streaming_chunks: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="chat-log", markup=True, wrap=True, auto_scroll=True)
        yield Static("", id="status")
        yield Input(placeholder="Type a message...", id="chat-input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        inp = self.query_one("#chat-input", Input)
        inp.value = ""

        cmd = text.split()[0].lower()

        # TUI-local commands
        if cmd in _TUI_COMMANDS:
            self._handle_tui_command(cmd)
            return

        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold cyan]You:[/bold cyan] {text}")

        # Server commands that return instantly (no LLM call)
        if cmd in _SERVER_COMMANDS or cmd == "/resume":
            response = self._server.handle_message(SENDER_ID, text)
            self._append_response(response)
            self._update_subtitle()
            return

        # Normal message — send to agent
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
        self.call_from_thread(self._show_status, "Thinking...")
        self._streaming_chunks = []

        def on_delta(chunk: str) -> None:
            if not self._streaming_chunks:
                # First chunk — swap status bar for streaming buffer
                self.call_from_thread(self._hide_status)
                self.call_from_thread(self._start_streaming)
            self._streaming_chunks.append(chunk)
            self.call_from_thread(self._update_streaming, "".join(self._streaming_chunks))

        try:
            response = self._server.handle_message(
                SENDER_ID, text, on_text_delta=on_delta,
            )
        except Exception as exc:
            response = f"[red]Error: {exc}[/red]"

        self.call_from_thread(self._hide_status)
        self.call_from_thread(self._finish_streaming, response)
        self.call_from_thread(self._enable_input)
        self.call_from_thread(self._update_subtitle)

    def _show_status(self, text: str) -> None:
        status = self.query_one("#status", Static)
        status.update(f"  ⏳ {text}")
        status.display = True

    def _hide_status(self) -> None:
        status = self.query_one("#status", Static)
        status.display = False

    def _start_streaming(self) -> None:
        """Mount a temporary Static widget as a streaming buffer."""
        buf = Static("", id="streaming-buffer", markup=True)
        log = self.query_one("#chat-log", RichLog)
        self.mount(buf, after=log)

    def _update_streaming(self, full_text: str) -> None:
        """Update the streaming buffer content in-place."""
        try:
            buf = self.query_one("#streaming-buffer", Static)
        except Exception:
            # Widget removed between delta and UI update — safe to ignore
            return
        buf.update(f"[bold green]Assistant:[/bold green] {full_text}")

    def _finish_streaming(self, final_text: str) -> None:
        """Remove streaming buffer and write final text to RichLog."""
        try:
            buf = self.query_one("#streaming-buffer", Static)
        except Exception:
            # Widget already removed — nothing to clean up
            pass
        else:
            buf.remove()
        self._append_response(final_text)

    def _append_response(self, text: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold green]Assistant:[/bold green] {text}")
        log.write("")

    def _enable_input(self) -> None:
        inp = self.query_one("#chat-input", Input)
        inp.disabled = False
        inp.focus()

    def _replay_history(self) -> None:
        """Render recent messages from the active session into the RichLog."""
        session = self._server._session_mgr.get_or_create(SENDER_ID)
        log = self.query_one("#chat-log", RichLog)

        messages = session.messages[-20:]
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str):
                log.write(f"[bold cyan]You:[/bold cyan] {content}")
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
                    log.write(f"[bold green]Assistant:[/bold green] {text}")
                    log.write("")

    def _update_subtitle(self) -> None:
        session = self._server._session_mgr.get_or_create(SENDER_ID)
        title = session.title or "(new session)"
        self.sub_title = f"Session {session.session_id}: {title}"

    def action_new_session(self) -> None:
        session = self._server._session_mgr.new_session(SENDER_ID)
        log = self.query_one("#chat-log", RichLog)
        log.clear()
        log.write(f"[dim]Started new session {session.session_id}.[/dim]")
        self._update_subtitle()

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
