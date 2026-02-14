"""Chat server - wires channels, sessions, and the agent loop together."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from taskrunner.agent import run_agent_loop
from taskrunner.approvals import ApprovalQueue
from taskrunner.log import generate_request_id, request_id_var
from taskrunner.memory import MemoryManager
from taskrunner.models import AgentDefinition
from taskrunner.prompt_builder import build_system_prompt
from taskrunner.session import SessionManager
from taskrunner.tools import execute_tool_call

logger = logging.getLogger(__name__)

# Special commands handled before the agent loop
_CLEAR_COMMANDS = {"clear", "reset", "/clear", "/reset"}

# Approval response patterns
_APPROVE_WORDS = {"y", "yes"}
_DENY_WORDS = {"n", "no"}


class ChatServer:
    """Connects a channel to the agent loop via session management."""

    def __init__(
        self,
        agent_def: AgentDefinition,
        use_containers: bool = False,
        imessage_channel: object | None = None,
        confirm_fn: object | None = None,
    ):
        self._agent_def = agent_def
        self._use_containers = use_containers
        self._imessage_channel = imessage_channel
        self._confirm_fn = confirm_fn
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

        # Initialize approval queue
        approvals_dir = "approvals"
        if agent_def.guardian and agent_def.guardian.review:
            approvals_dir = getattr(agent_def.guardian.review, "approvals_dir", approvals_dir)
        self._approval_queue = ApprovalQueue(approvals_dir=approvals_dir)

        # Initialize guardian if configured and enabled
        self._guardian = None
        if agent_def.guardian and agent_def.guardian.enabled:
            from guardian import Guardian

            self._guardian = Guardian(agent_def.guardian)
            self._guardian.warm_up()
            logger.info("Guardian enabled")

    def handle_message(self, sender_id: str, text: str) -> str:
        """Process an incoming message and return a response."""
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

        # Check for pending approval response BEFORE normal processing
        if stripped.lower() in _APPROVE_WORDS | _DENY_WORDS:
            pending = self._approval_queue.get_pending(sender_id)
            if pending is not None:
                return self._handle_approval_response(
                    sender_id, pending, stripped.lower() in _APPROVE_WORDS,
                )

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

        # Handle approval_required — queue the action and notify
        if result.stop_reason == "approval_required" and result.pending_approval:
            pa = result.pending_approval
            action = self._approval_queue.add(
                sender_id=sender_id,
                tool_name=pa.tool_name,
                tool_input=pa.tool_input,
                reason=pa.reason,
            )
            self._send_approval_request(sender_id, action)
            # Save session state
            self._session_mgr.save_session(session)
            return "⏳ Waiting for your approval to proceed."

        # Save the updated messages (agent loop mutates the list)
        self._session_mgr.save_session(session)

        return result.text

    def _send_approval_request(self, sender_id: str, action) -> None:
        """Send an approval request message via iMessage (or log it)."""
        # Format args summary
        args_lines = []
        for k, v in action.tool_input.items():
            v_str = str(v)
            if len(v_str) > 80:
                v_str = v_str[:77] + "..."
            args_lines.append(f"  {k}: {v_str}")
        args_summary = "\n".join(args_lines) if args_lines else "  (none)"

        msg = (
            f"⚠️ Action requires approval:\n\n"
            f"🔧 Tool: {action.tool_name}\n"
            f"📋 Args:\n{args_summary}\n"
            f"📝 Reason: {action.policy_reason}\n\n"
            f"Reply **Y** to approve or **N** to deny."
        )

        if self._imessage_channel and self._agent_def.channels.imessage:
            recipient = self._agent_def.channels.imessage.listen_to
            try:
                self._imessage_channel.send(recipient, msg)
            except Exception:
                logger.exception("Failed to send approval request via iMessage")
        else:
            logger.info("Approval request (no iMessage channel): %s", msg)

    def _handle_approval_response(
        self, sender_id: str, pending, approved: bool,
    ) -> str:
        """Resolve a pending action and execute if approved."""
        self._approval_queue.resolve(pending.id, approved)

        if not approved:
            result_msg = f"❌ Action denied: {pending.tool_name}"
            self._send_imessage(sender_id, result_msg)
            return result_msg

        # Execute the tool
        try:
            tool_result = execute_tool_call(
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
                tools_config=self._agent_def.tools,
                use_containers=self._use_containers,
            )
            result_msg = f"✅ Approved and executed: {pending.tool_name}\n\nResult:\n{tool_result}"
        except Exception as e:
            logger.exception("Tool execution failed after approval")
            result_msg = f"✅ Approved but execution failed: {e}"

        self._send_imessage(sender_id, result_msg)
        return result_msg

    def _send_imessage(self, sender_id: str, msg: str) -> None:
        """Send a message via iMessage if available."""
        if self._imessage_channel and self._agent_def.channels.imessage:
            recipient = self._agent_def.channels.imessage.listen_to
            try:
                self._imessage_channel.send(recipient, msg)
            except Exception:
                logger.exception("Failed to send iMessage")

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
