"""Chat server - wires channels, sessions, and the agent loop together."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from taskrunner.agent import run_agent_loop
from taskrunner.log import generate_request_id, request_id_var
from taskrunner.memory import MemoryManager
from taskrunner.models import AgentDefinition
from taskrunner.prompt_builder import build_system_prompt
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
        imessage_channel: object | None = None,
    ):
        self._agent_def = agent_def
        self._use_containers = use_containers
        self._imessage_channel = imessage_channel

        # If no explicit confirm_fn but we have an iMessage channel, use iMessage approval
        if confirm_fn is not None:
            self._confirm_fn = confirm_fn
        elif imessage_channel is not None and agent_def.channels.imessage:
            self._confirm_fn = self._imessage_confirm_action
        else:
            self._confirm_fn = confirm_fn  # None — agent will fail-closed
        self._session_mgr = SessionManager(
            sessions_dir=agent_def.session.sessions_dir,
            max_history=agent_def.session.max_history,
        )

        # Initialize memory manager if workspace is configured
        self._memory: MemoryManager | None = None
        ws_path = Path(agent_def.workspace.path)
        if ws_path.is_dir():
            self._memory = MemoryManager(
                workspace_dir=agent_def.workspace.path,
                timezone_name=agent_def.workspace.timezone,
            )
            logger.info("Memory system enabled (workspace: %s)", agent_def.workspace.path)

        # Initialize guardian if configured and enabled
        self._guardian = None
        if agent_def.guardian and agent_def.guardian.enabled:
            from guardian import Guardian

            self._guardian = Guardian(agent_def.guardian)
            self._guardian.warm_up()
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

        # Build system prompt using the prompt builder
        system_prompt = self._build_system_prompt()

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
            memory_manager=self._memory,
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

    def _imessage_confirm_action(self, tool_name: str, tool_input: dict, reason: str) -> bool:
        """Request approval via iMessage and wait for reply."""
        if not self._imessage_channel or not self._agent_def.channels.imessage:
            logger.warning("iMessage confirm called but no channel available — denying")
            return False

        recipient = self._agent_def.channels.imessage.listen_to
        timeout = 60
        if self._agent_def.guardian and self._agent_def.guardian.review:
            timeout = self._agent_def.guardian.review.timeout_seconds

        # Format args summary (truncate long values)
        args_lines = []
        for k, v in tool_input.items():
            v_str = str(v)
            if len(v_str) > 80:
                v_str = v_str[:77] + "..."
            args_lines.append(f"  {k}: {v_str}")
        args_summary = "\n".join(args_lines) if args_lines else "  (none)"

        msg = (
            f"⚠️ Action requires approval:\n\n"
            f"🔧 Tool: {tool_name}\n"
            f"📋 Args:\n{args_summary}\n"
            f"📝 Reason: {reason}\n\n"
            f"Reply Y to approve, N to deny (auto-denies in {timeout}s)"
        )

        try:
            self._imessage_channel.send(recipient, msg)
        except Exception:
            logger.exception("Failed to send approval request — denying")
            return False

        reply = self._imessage_channel.wait_for_reply(recipient, timeout_seconds=timeout)
        if reply and reply.strip().lower() in ("y", "yes"):
            logger.info("User approved action %s via iMessage", tool_name)
            return True

        logger.info("User denied/timed out action %s via iMessage", tool_name)
        return False

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

    def _build_system_prompt(self) -> str:
        """Build the system prompt from workspace files, memory, and config.

        This mirrors OpenClaw's pattern of assembling the system prompt from
        multiple sources each run, rather than using a static string.
        """
        ws_cfg = self._agent_def.workspace

        # Load base prompt from file if configured, else use inline
        base_prompt = self._agent_def.system_prompt
        if self._agent_def.system_prompt_file:
            prompt_path = Path(self._agent_def.system_prompt_file)
            if prompt_path.exists():
                base_prompt = prompt_path.read_text().strip()

        # Get memory context
        memory_context = None
        if self._memory:
            memory_context = self._memory.get_recent_context(
                days=ws_cfg.memory_days,
                max_chars=ws_cfg.memory_max_chars,
            )

        return build_system_prompt(
            base_prompt=base_prompt,
            workspace_dir=ws_cfg.path,
            timezone_name=ws_cfg.timezone,
            tools_config=self._agent_def.tools,
            memory_context=memory_context,
            max_chars_per_file=ws_cfg.max_chars_per_file,
        )
