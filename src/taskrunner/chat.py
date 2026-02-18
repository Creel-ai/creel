"""Chat server - wires channels, sessions, and the agent loop together."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from taskrunner.agent import run_agent_loop
from taskrunner.approvals import ApprovalQueue
from taskrunner.log import generate_request_id, request_id_var
from taskrunner.memory import MemoryManager
from taskrunner.models import AgentDefinition
from taskrunner.prompt_builder import build_system_prompt
from taskrunner.quiet_hours import should_suppress
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

    _start_time: datetime  # server start time for uptime tracking

    def __init__(
        self,
        agent_def: AgentDefinition,
        use_containers: bool = False,
        reply_channel: object | None = None,
        confirm_fn: object | None = None,
        # Backward compat alias
        imessage_channel: object | None = None,
    ):
        self._agent_def = agent_def
        self._use_containers = use_containers
        self._start_time = datetime.now(timezone.utc)
        self._reply_channel = reply_channel or imessage_channel
        self._confirm_fn = confirm_fn
        # Build summarize_fn if summarization is enabled
        summarize_fn = None
        if agent_def.session.summarize_on_trim:
            def _do_summarize(messages: list[dict]) -> str:
                from taskrunner.llm import summarize_messages
                if agent_def.llm.secrets:
                    from taskrunner.orchestrator import _load_secrets_to_env
                    _load_secrets_to_env(agent_def.llm.secrets)
                return summarize_messages(
                    messages,
                    model=agent_def.session.summary_model,
                    max_tokens=agent_def.session.summary_max_tokens,
                    use_container=use_containers,
                )
            summarize_fn = _do_summarize

        self._session_mgr = SessionManager(
            sessions_dir=agent_def.session.sessions_dir,
            max_history=agent_def.session.max_history,
            ttl_hours=agent_def.session.ttl_hours,
            summarize_on_trim=agent_def.session.summarize_on_trim,
            summarize_fn=summarize_fn,
            max_context_tokens=agent_def.session.max_context_tokens,
        )

        # Initialize memory manager if workspace is configured
        self._memory: MemoryManager | None = None
        ws_path = Path(agent_def.workspace.path)
        if ws_path.is_dir():
            self._memory = MemoryManager(
                workspace_dir=agent_def.workspace.path,
                timezone_name=agent_def.workspace.timezone,
                max_daily_entries=agent_def.workspace.max_daily_entries,
                max_long_term_lines=agent_def.workspace.max_long_term_lines,
            )
            self._memory.compact_daily_files(
                days_to_keep=agent_def.workspace.compact_after_days,
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

    def handle_message(
        self,
        sender_id: str,
        text: str,
        on_text_delta: Callable[[str], None] | None = None,
        *,
        auto_approve: bool = False,
    ) -> str:
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

        # Handle /status — show server status info
        if stripped.lower() == "/status":
            return self._format_status(sender_id)

        # Handle /model — show current model config
        if stripped.lower() == "/model":
            return self._format_model()

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

        # Build the confirm_action callback for this request.
        # --auto-approve from `creel send` provides a callback that always
        # approves, so REVIEW-verdict tools execute immediately instead of
        # being queued for async approval the CLI caller can never answer.
        confirm_action = self._confirm_fn
        if auto_approve and confirm_action is not None:
            logger.debug("auto_approve requested but confirm_fn already set; using existing confirm_fn")
        elif auto_approve:
            def _auto_confirm(tool_name: str, tool_input: dict, reason: str) -> bool:
                logger.info("Auto-approving %s (reason: %s)", tool_name, reason)
                if self._guardian is not None:
                    self._guardian.log_action_outcome(tool_name, "review", "auto_approved_by_cli")
                return True
            confirm_action = _auto_confirm

        # Run the agent loop (containerized or direct)
        if self._use_containers:
            from taskrunner.container_agent import run_agent_loop_container

            result = run_agent_loop_container(
                messages=session.messages,
                llm_config=self._agent_def.llm,
                tools_config=self._agent_def.tools,
                agent_config=self._agent_def.agent,
                system_prompt=system_prompt,
                use_containers=self._use_containers,
                guardian=self._guardian,
                confirm_action=confirm_action,
                memory_manager=self._memory,
                bridge_config=self._agent_def.bridge,
            )
        else:
            result = run_agent_loop(
                messages=session.messages,
                llm_config=self._agent_def.llm,
                tools_config=self._agent_def.tools,
                agent_config=self._agent_def.agent,
                system_prompt=system_prompt,
                use_containers=self._use_containers,
                guardian=self._guardian,
                confirm_action=confirm_action,
                memory_manager=self._memory,
                on_text_delta=on_text_delta,
                bridge_config=self._agent_def.bridge,
            )

        logger.info(
            "Agent response for %s: %d chars, %d turns, %d tool calls (%s)",
            sender_id,
            len(result.text),
            result.turns_used,
            result.tool_calls_made,
            result.stop_reason,
        )

        # Update token count for session compaction
        last_tokens = getattr(result, "last_input_tokens", 0)
        if isinstance(last_tokens, int) and last_tokens > 0:
            self._session_mgr.update_token_count(sender_id, last_tokens)

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
        # Check quiet hours before sending proactive approval request
        if should_suppress(self._agent_def.quiet_hours, urgent=False):
            logger.info("Approval request suppressed due to quiet hours")
            return

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

        if self._reply_channel:
            try:
                self._reply_channel.send(sender_id, msg)
            except Exception:
                logger.exception("Failed to send approval request via channel")
        else:
            logger.info("Approval request (no reply channel): %s", msg)

    def _handle_approval_response(
        self, sender_id: str, pending, approved: bool,
    ) -> str:
        """Resolve a pending action and execute if approved."""
        self._approval_queue.resolve(pending.id, approved)

        if not approved:
            result_msg = f"❌ Action denied: {pending.tool_name}"
            self._send_reply(sender_id, result_msg, proactive=False)  # Direct reply to approval
            return result_msg

        # Execute the tool
        try:
            tool_result = execute_tool_call(
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
                tools_config=self._agent_def.tools,
                use_containers=self._use_containers,
                bridge_config=self._agent_def.bridge,
            )
            result_msg = f"✅ Approved and executed: {pending.tool_name}\n\nResult:\n{tool_result}"
        except Exception as e:
            logger.exception("Tool execution failed after approval")
            result_msg = f"✅ Approved but execution failed: {e}"

        self._send_reply(sender_id, result_msg, proactive=False)  # Direct reply to approval
        return result_msg

    def _send_reply(self, sender_id: str, msg: str, proactive: bool = False, urgent: bool = False) -> None:
        """Send a message via the reply channel if available.

        Args:
            sender_id: The sender ID
            msg: Message content
            proactive: True if this is a proactive notification, False for direct replies
            urgent: True if message is marked urgent
        """
        # Check quiet hours for proactive messages only
        if proactive and should_suppress(self._agent_def.quiet_hours, urgent=urgent):
            logger.info("Message suppressed due to quiet hours (proactive=%s, urgent=%s)", proactive, urgent)
            return

        if self._reply_channel:
            try:
                self._reply_channel.send(sender_id, msg)
            except Exception:
                logger.exception("Failed to send message via reply channel")

    def get_or_create_session(self, sender_id: str):
        """Get the active session for a sender, creating one if needed."""
        return self._session_mgr.get_or_create(sender_id)

    def new_session(self, sender_id: str):
        """Create and activate a new session for the sender."""
        return self._session_mgr.new_session(sender_id)

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

    def _format_status(self, sender_id: str) -> str:
        """Format server status information."""
        session = self._session_mgr.get_or_create(sender_id)
        uptime = datetime.now(timezone.utc) - self._start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)

        guardian_status = "disabled"
        if self._guardian:
            guardian_status = "enabled"

        lines = [
            "Status:",
            f"  Model: {self._agent_def.llm.model}",
            f"  Session ID: {session.session_id}",
            f"  Messages: {len(session.messages)}",
            f"  Uptime: {hours}h {minutes}m {seconds}s",
            f"  Guardian: {guardian_status}",
        ]
        return "\n".join(lines)

    def _format_model(self) -> str:
        """Format current model configuration."""
        llm = self._agent_def.llm
        agent = self._agent_def.agent
        lines = [
            "Model:",
            f"  Name: {llm.model}",
            f"  Max tokens: {llm.max_tokens}",
            f"  Max turns: {agent.max_turns}",
        ]
        return "\n".join(lines)

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
            # Screen memory context for stored injection payloads
            if memory_context and self._guardian:
                screen_result = self._guardian.screen_input(memory_context)
                if screen_result.blocked:
                    logger.warning(
                        "Guardian blocked memory context from system prompt "
                        "(confidence=%.3f)",
                        screen_result.classifier_result.confidence
                        if screen_result.classifier_result
                        else 0.0,
                    )
                    memory_context = None

        return build_system_prompt(
            base_prompt=base_prompt,
            workspace_dir=ws_cfg.path,
            timezone_name=ws_cfg.timezone,
            tools_config=self._agent_def.tools,
            memory_context=memory_context,
            max_chars_per_file=ws_cfg.max_chars_per_file,
        )
