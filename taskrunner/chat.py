"""Chat server - wires channels, sessions, and the agent loop together."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from taskrunner.agent import run_agent_loop
from taskrunner.log import generate_request_id, request_id_var
from taskrunner.models import AgentDefinition
from taskrunner.session import SessionManager

logger = logging.getLogger(__name__)

# Special commands handled before the agent loop
_CLEAR_COMMANDS = {"clear", "reset", "/clear", "/reset"}


class ChatServer:
    """Connects a channel to the agent loop via session management."""

    def __init__(
        self,
        agent_def: AgentDefinition,
        use_containers: bool = False,
        confirm_fn: Callable[[str, dict, str], bool] | None = None,
    ):
        self._agent_def = agent_def
        self._use_containers = use_containers
        self._confirm_fn = confirm_fn
        self._session_mgr = SessionManager(
            sessions_dir=agent_def.session.sessions_dir,
            max_history=agent_def.session.max_history,
        )

        # Initialize guardian if configured and enabled
        self._guardian = None
        if agent_def.guardian and agent_def.guardian.enabled:
            from guardian import Guardian

            self._guardian = Guardian(agent_def.guardian)
            logger.info("Guardian enabled")

    def handle_message(self, sender_id: str, text: str) -> str:
        """Process an incoming message and return a response.

        This is the callback passed to channels.
        """
        # Set request ID for this message
        rid = generate_request_id()
        request_id_var.set(rid)
        logger.info("Handling message from %s [request_id=%s]", sender_id, rid)

        stripped = text.strip()

        # Handle clear command
        if stripped.lower() in _CLEAR_COMMANDS:
            self._session_mgr.clear(sender_id)
            return "Session cleared."

        # Handle /new — start a new session
        if stripped.lower() == "/new":
            session = self._session_mgr.new_session(sender_id)
            return f"Started new session {session.session_id}."

        # Handle /sessions — list all sessions
        if stripped.lower() == "/sessions":
            return self._format_sessions_list(sender_id)

        # Handle /resume <id> — resume a session
        if stripped.lower().startswith("/resume"):
            return self._handle_resume(sender_id, stripped)

        # Screen input through guardian (before adding to session)
        if self._guardian:
            screen_result = self._guardian.screen_input(text)
            if screen_result.blocked:
                logger.warning("Guardian blocked input from %s", sender_id)
                return screen_result.rejection_message

        # Add user message and get session with history
        session = self._session_mgr.add_user_message(sender_id, text)

        # Build system prompt with current date
        now = datetime.now(timezone.utc)
        system_prompt = self._agent_def.system_prompt.format(
            date=now.strftime("%A, %B %d, %Y"),
        )

        # Load LLM secrets if configured
        if self._agent_def.llm.secrets:
            from taskrunner.orchestrator import _load_secrets_to_env
            _load_secrets_to_env(self._agent_def.llm.secrets)

        # Run the agent loop
        result = run_agent_loop(
            messages=session.messages,
            llm_config=self._agent_def.llm,
            tools_config=self._agent_def.tools,
            agent_config=self._agent_def.agent,
            system_prompt=system_prompt,
            use_containers=self._use_containers,
            guardian=self._guardian,
            confirm_action=self._confirm_fn,
        )

        logger.info(
            "Agent response for %s: %d chars, %d turns, %d tool calls (%s)",
            sender_id,
            len(result.text),
            result.turns_used,
            result.tool_calls_made,
            result.stop_reason,
        )

        # Save the updated messages (agent loop mutates the list)
        self._session_mgr._save(session)

        return result.text

    def _format_sessions_list(self, sender_id: str) -> str:
        """Format the sessions list for display."""
        sessions = self._session_mgr.list_sessions(sender_id)
        if not sessions:
            return "No sessions found."

        active_id = self._session_mgr._get_active_session_id(sender_id)
        lines = ["Sessions:", ""]
        for s in sessions:
            marker = " *" if s["session_id"] == active_id else ""
            dt = datetime.fromtimestamp(s["last_active"], tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
            title = s["title"] or "(untitled)"
            lines.append(
                f"  {s['session_id']}{marker}  {title}  "
                f"({s['message_count']} msgs, {date_str})"
            )
        lines.append("")
        lines.append("* = active session. Use /resume <id> to switch.")
        return "\n".join(lines)

    def _handle_resume(self, sender_id: str, text: str) -> str:
        """Handle the /resume <id> command."""
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return "Usage: /resume <session_id>"
        session_id = parts[1].strip()
        try:
            session = self._session_mgr.resume_session(sender_id, session_id)
            title = session.title or "(untitled)"
            return f"Resumed session {session_id}: {title}"
        except ValueError as e:
            return str(e)
