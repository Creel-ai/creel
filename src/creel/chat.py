"""Chat server - wires channels, sessions, and the agent loop together."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creel.agent import run_agent_loop
from creel.approvals import ApprovalQueue
from creel.channels.message import Attachment, AttachmentType
from creel.log import generate_request_id, request_id_var
from creel.memory import MemoryManager
from creel.models import AgentDefinition
from creel.prompt_builder import build_system_prompt
from creel.quiet_hours import should_suppress
from creel.services.media_store import MediaStore
from creel.services.transcription import TranscriptionService
from creel.services.vision import VisionProcessor
from creel.session import SessionManager
from creel.subagents import SubAgentManager
from creel.tools import execute_tool_call

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
        reply_channel: Any | None = None,
        confirm_fn: Callable[[str, dict, str], bool] | None = None,
        # Backward compat alias
        imessage_channel: Any | None = None,
        cron_manager: object | None = None,
    ):
        self._agent_def = agent_def
        self._use_containers = use_containers
        self._cron_manager = cron_manager
        self._start_time = datetime.now(UTC)
        self._reply_channel = reply_channel or imessage_channel
        self._confirm_fn = confirm_fn

        # Initialize warm container pool if using containers
        self._container_pool = None
        if use_containers:
            from creel.container_pool import ContainerPool, ContainerPoolConfig

            pool_config = agent_def.llm.container_pool
            self._container_pool = ContainerPool(
                ContainerPoolConfig(
                    enabled=pool_config.enabled,
                    idle_timeout_seconds=pool_config.idle_timeout_seconds,
                    max_containers=pool_config.max_containers,
                )
            )
            if pool_config.enabled:
                logger.info(
                    "Container pool enabled (idle_timeout=%ds, max=%d)",
                    pool_config.idle_timeout_seconds,
                    pool_config.max_containers,
                )
        # Build summarize_fn if summarization is enabled
        summarize_fn = None
        if agent_def.session.summarize_on_trim:

            def _do_summarize(messages: list[dict]) -> str:
                from creel.llm import summarize_messages

                if agent_def.llm.secrets:
                    from creel.orchestrator import _load_secrets_to_env

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

        # Per-sender session state (e.g. workspace path for file_ops)
        self._session_states: dict[str, dict] = {}

        # Rate limiter for inject_system_event: per-sender list of timestamps.
        # Note: stale sender entries are never pruned from this dict. This is
        # fine because only cron_sender_id typically injects events, so the
        # dict stays small. If many unique senders start injecting events,
        # consider periodic cleanup.
        self._event_injection_times: dict[str, list[float]] = {}
        self._max_events_per_minute: int = 10

        # Initialize approval queue
        # Keep approval state scoped with session storage by default so tests
        # and multi-instance deployments don't share a global pending queue.
        default_approvals_dir = str(Path(agent_def.session.sessions_dir).parent / "approvals")
        approvals_dir = default_approvals_dir
        if agent_def.guardian and agent_def.guardian.review:
            configured = getattr(agent_def.guardian.review, "approvals_dir", "approvals")
            # Preserve explicit custom dirs; map the legacy default ("approvals")
            # to a session-scoped location for isolation.
            if configured and configured != "approvals":
                approvals_dir = configured
        self._approval_queue = ApprovalQueue(approvals_dir=approvals_dir)

        # Initialize guardian if configured and enabled
        self._guardian = None
        if agent_def.guardian and agent_def.guardian.enabled:
            from guardian import Guardian

            self._guardian = Guardian(agent_def.guardian)
            self._guardian.warm_up()
            logger.info("Guardian enabled")

        # Initialize media services for attachment processing
        media_cfg = agent_def.media
        if media_cfg is not None and media_cfg.enabled:
            storage_dir = Path(media_cfg.storage_dir).expanduser()
            self._media_store: MediaStore | None = MediaStore(
                base_dir=storage_dir,
                max_file_size=media_cfg.max_file_size_mb * 1024 * 1024,
                retention_days=media_cfg.retention_days,
            )
            self._transcription: TranscriptionService | None = TranscriptionService(
                backend=media_cfg.transcription.backend,
                model=media_cfg.transcription.model,
                api_key=media_cfg.transcription.api_key,
            )
            self._vision: VisionProcessor | None = VisionProcessor(
                provider="anthropic",
                max_pixels=media_cfg.vision.max_pixels,
                quality=media_cfg.vision.quality,
            )
            logger.info("Media services enabled (storage: %s)", storage_dir)
        else:
            self._media_store = None
            self._transcription = None
            self._vision = None

        # Initialize sub-agent manager
        self._subagent_manager = SubAgentManager(
            llm_config=agent_def.llm,
            tools_config=agent_def.tools,
            agent_config=agent_def.agent,
            system_prompt=None,  # built lazily per request
            use_containers=use_containers,
            guardian=self._guardian,
            bridge_config=agent_def.bridge,
            result_callback=self._on_subagent_result,
        )

    def handle_message(
        self,
        sender_id: str,
        text: str,
        on_text_delta: Callable[[str], None] | None = None,
        *,
        attachments: list[Attachment] | None = None,
        channel: str = "unknown",
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
            self._session_states.pop(sender_id, None)
            return "Session cleared."

        # Handle /new — start a new session
        if stripped.lower() == "/new":
            session = self._session_mgr.new_session(sender_id)
            self._session_states.pop(sender_id, None)
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
                    sender_id,
                    pending,
                    stripped.lower() in _APPROVE_WORDS,
                )

        # Screen input through guardian (before adding to session)
        if self._guardian:
            screen_result = self._guardian.screen_input(text)
            if screen_result.blocked:
                logger.warning("Guardian blocked input from %s", sender_id)
                return screen_result.rejection_message

        # Do not start a new LLM turn while an approval is pending.
        pending = self._approval_queue.get_pending(sender_id)
        if pending is not None:
            self._send_approval_request(sender_id, pending)
            return (
                f"⏳ Approval still pending for `{pending.tool_name}`. "
                "Reply Y to approve or N to deny."
            )

        # Process media attachments (voice transcription + image vision)
        text, image_content_blocks = self._process_attachments(
            text,
            attachments,
            sender_id,
            channel=channel,
        )

        # Add user message: use content blocks when images are present
        if image_content_blocks:
            content_blocks: list[dict] = [{"type": "text", "text": text}]
            content_blocks.extend(image_content_blocks)
            session = self._session_mgr.add_user_message_blocks(
                sender_id,
                content_blocks,
            )
        else:
            session = self._session_mgr.add_user_message(sender_id, text)

        # Build system prompt using the prompt builder
        system_prompt = self._build_system_prompt()

        # Load LLM secrets if configured
        if self._agent_def.llm.secrets:
            from creel.orchestrator import _load_secrets_to_env

            _load_secrets_to_env(self._agent_def.llm.secrets)

        # Build the confirm_action callback for this request.
        # --auto-approve from `creel send` provides a callback that always
        # approves, so REVIEW-verdict tools execute immediately instead of
        # being queued for async approval the CLI caller can never answer.
        confirm_action = self._confirm_fn
        if auto_approve and confirm_action is not None:
            logger.debug(
                "auto_approve requested but confirm_fn already set; using existing confirm_fn"
            )
        elif auto_approve:

            def _auto_confirm(tool_name: str, tool_input: dict, reason: str) -> bool:
                logger.info("Auto-approving %s (reason: %s)", tool_name, reason)
                if self._guardian is not None:
                    self._guardian.log_action_outcome(tool_name, "review", "auto_approved_by_cli")
                return True

            confirm_action = _auto_confirm

        # Look up per-sender session state (workspace path, etc.)
        session_state = self._session_states.setdefault(sender_id, {})
        session_state["sender_id"] = sender_id

        # Run the agent loop (containerized or direct)
        if self._use_containers:
            from creel.container_agent import run_agent_loop_container

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
                session_state=session_state,
                container_pool=self._container_pool,
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
                session_state=session_state,
                cron_manager=self._cron_manager,
                subagent_manager=self._subagent_manager,
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
                tool_use_id=pa.tool_use_id,
            )
            self._send_approval_request(sender_id, action)
            # Save session state
            self._session_mgr.save_session(session)
            return "⏳ Waiting for your approval to proceed."

        # Save the updated messages (agent loop mutates the list)
        self._session_mgr.save_session(session)

        return result.text

    def _process_attachments(
        self,
        text: str,
        attachments: list[Attachment] | None,
        sender_id: str,
        channel: str = "unknown",
    ) -> tuple[str, list[dict]]:
        """Process media attachments, returning updated text and image content blocks.

        Voice/audio attachments are transcribed and their text is prepended to the
        user message. Image attachments are converted to LLM content blocks.

        Returns:
            (updated_text, image_content_blocks) — text with transcriptions prepended
            and a list of image content block dicts for the LLM.
        """
        if not attachments:
            return text, []

        if self._media_store is None:
            logger.info("Media disabled — ignoring %d attachment(s)", len(attachments))
            return text, []

        voice_parts: list[str] = []
        image_blocks: list[dict] = []

        for attachment in attachments:
            if attachment.type in (AttachmentType.VOICE, AttachmentType.AUDIO):
                self._process_voice_attachment(attachment, sender_id, voice_parts, channel)
            elif attachment.type == AttachmentType.IMAGE:
                self._process_image_attachment(attachment, sender_id, image_blocks, channel)

        # Prepend transcribed voice text to the user message
        if voice_parts:
            voice_text = "\n".join(voice_parts)
            text = f"{voice_text}\n{text}" if text else voice_text

        return text, image_blocks

    def _process_voice_attachment(
        self,
        attachment: Attachment,
        sender_id: str,
        voice_parts: list[str],
        channel: str = "unknown",
    ) -> None:
        """Save, transcribe a voice attachment and append to voice_parts."""
        assert self._media_store is not None  # guarded by _process_attachments
        assert self._transcription is not None
        try:
            saved_path = self._media_store.save_media(attachment, channel=channel)
        except Exception:
            logger.warning("Failed to save voice attachment", exc_info=True)
            voice_parts.append("[Voice message: could not save audio file]")
            return

        transcribed = self._transcription.transcribe(saved_path)
        if transcribed:
            voice_parts.append(f"[Voice message]: {transcribed}")
        else:
            voice_parts.append(f"[Voice message: transcription failed] (file: {saved_path.name})")

    def _process_image_attachment(
        self,
        attachment: Attachment,
        sender_id: str,
        image_blocks: list[dict],
        channel: str = "unknown",
    ) -> None:
        """Save an image attachment and prepare it as an LLM content block."""
        assert self._media_store is not None  # guarded by _process_attachments
        assert self._vision is not None
        try:
            saved_path = self._media_store.save_media(attachment, channel=channel)
        except Exception:
            logger.warning("Failed to save image attachment", exc_info=True)
            return

        content_block = self._vision.prepare_image(saved_path)
        if content_block is not None:
            image_blocks.append(content_block)
        else:
            logger.warning("Image could not be processed: %s", saved_path)

    def _on_subagent_result(self, agent_id: str, result_text: str) -> None:
        """Callback fired when a sub-agent completes, fails, or times out.

        Injects a system event into the parent sender's session so the LLM
        sees the result on the next interaction.
        """
        info = self._subagent_manager.get(agent_id)
        label = info.label if info else agent_id
        status = info.status.value if info else "unknown"
        if info and info.error:
            event = f"[Sub-agent '{label}' {status}] {info.error}"
        else:
            summary = result_text[:500] + "..." if len(result_text) > 500 else result_text
            event = f"[Sub-agent '{label}' {status}] {summary}"
        logger.info("Sub-agent %s result: %s", agent_id, status)
        sender_id = info.sender_id if info else ""
        if sender_id:
            self.inject_system_event(sender_id, event)

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
        self,
        sender_id: str,
        pending,
        approved: bool,
    ) -> str:
        """Resolve a pending action, execute if approved, and resume the agent loop."""
        self._approval_queue.resolve(pending.id, approved)

        session = self._session_mgr.get_or_create(sender_id)

        if not approved:
            # Append a user message so the LLM can acknowledge the denial
            self._session_mgr.add_user_message(
                sender_id,
                f"[SYSTEM] The user denied the action '{pending.tool_name}': {pending.policy_reason}",
            )
            self._session_mgr.save_session(session)
            return f"❌ Action denied: {pending.tool_name}"

        # Execute the approved tool
        session_state = self._session_states.setdefault(sender_id, {})
        session_state["sender_id"] = sender_id
        is_error = False
        try:
            tool_result = execute_tool_call(
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
                tools_config=self._agent_def.tools,
                use_containers=self._use_containers,
                memory_manager=self._memory,
                bridge_config=self._agent_def.bridge,
                session_state=session_state,
                cron_manager=self._cron_manager,
                subagent_manager=self._subagent_manager,
            )
        except Exception as e:
            logger.exception("Tool execution failed after approval")
            tool_result = f"Error: {e}"
            is_error = True

        # Patch the session messages: replace the synthetic error tool_result
        # (matching pending.tool_use_id) with the real execution result so
        # the LLM sees the actual output when we resume the agent loop.
        if pending.tool_use_id:
            self._patch_tool_result(session.messages, pending.tool_use_id, tool_result, is_error)

        # Resume the agent loop so the LLM can process the tool output
        system_prompt = self._build_system_prompt()

        if self._agent_def.llm.secrets:
            from creel.orchestrator import _load_secrets_to_env

            _load_secrets_to_env(self._agent_def.llm.secrets)

        if self._use_containers:
            from creel.container_agent import run_agent_loop_container

            result = run_agent_loop_container(
                messages=session.messages,
                llm_config=self._agent_def.llm,
                tools_config=self._agent_def.tools,
                agent_config=self._agent_def.agent,
                system_prompt=system_prompt,
                use_containers=self._use_containers,
                guardian=self._guardian,
                memory_manager=self._memory,
                bridge_config=self._agent_def.bridge,
                session_state=session_state,
                container_pool=self._container_pool,
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
                memory_manager=self._memory,
                bridge_config=self._agent_def.bridge,
                session_state=session_state,
                cron_manager=self._cron_manager,
                subagent_manager=self._subagent_manager,
            )

        # If the resumed loop itself hits another approval_required, queue it
        if result.stop_reason == "approval_required" and result.pending_approval:
            pa = result.pending_approval
            action = self._approval_queue.add(
                sender_id=sender_id,
                tool_name=pa.tool_name,
                tool_input=pa.tool_input,
                reason=pa.reason,
                tool_use_id=pa.tool_use_id,
            )
            self._send_approval_request(sender_id, action)
            self._session_mgr.save_session(session)
            return "⏳ Waiting for your approval to proceed."

        self._session_mgr.save_session(session)
        return result.text

    @staticmethod
    def _patch_tool_result(
        messages: list[dict],
        tool_use_id: str,
        result: str,
        is_error: bool,
    ) -> bool:
        """Replace a synthetic error tool_result with the real execution result.

        Searches backwards through messages for the tool_result matching
        *tool_use_id* and patches it in place. Returns True if patched.
        """
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("tool_use_id") == tool_use_id
                ):
                    block["content"] = result
                    block["is_error"] = is_error
                    return True
        return False

    def _send_reply(
        self, sender_id: str, msg: str, proactive: bool = False, urgent: bool = False
    ) -> None:
        """Send a message via the reply channel if available.

        Args:
            sender_id: The sender ID
            msg: Message content
            proactive: True if this is a proactive notification, False for direct replies
            urgent: True if message is marked urgent
        """
        # Check quiet hours for proactive messages only
        if proactive and should_suppress(self._agent_def.quiet_hours, urgent=urgent):
            logger.info(
                "Message suppressed due to quiet hours (proactive=%s, urgent=%s)",
                proactive,
                urgent,
            )
            return

        if self._reply_channel:
            try:
                self._reply_channel.send(sender_id, msg)
            except Exception:
                logger.exception("Failed to send message via reply channel")

    def inject_system_event(self, sender_id: str, text: str) -> None:
        """Inject a system event into a sender's active session.

        The event is wrapped with a [SYSTEM EVENT] prefix so the LLM can
        distinguish it from real user input.  Used by the cron subsystem
        for main-session jobs.

        Rate-limited to _max_events_per_minute per sender to prevent
        misfiring cron jobs from flooding a session.
        """
        now = time.monotonic()
        window = self._event_injection_times.setdefault(sender_id, [])
        # Prune entries older than 60s
        cutoff = now - 60
        window[:] = [t for t in window if t > cutoff]
        if len(window) >= self._max_events_per_minute:
            logger.warning(
                "Rate limit hit: dropping system event for %s (%d events in last 60s)",
                sender_id,
                len(window),
            )
            return
        window.append(now)

        wrapped = f"[SYSTEM EVENT]\n{text}"
        self._session_mgr.add_user_message(sender_id, wrapped)
        logger.info("Injected system event into session for %s", sender_id)

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
            dt = datetime.fromtimestamp(s["last_active"], tz=UTC)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
            title = s["title"] or "(untitled)"
            lines.append(
                f"  {s['session_id']}{marker}  {title}  ({s['message_count']} msgs, {date_str})"
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
        uptime = datetime.now(UTC) - self._start_time
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

    def shutdown(self) -> None:
        """Shut down the chat server and release resources."""
        if self._container_pool is not None:
            logger.info("Shutting down container pool")
            self._container_pool.shutdown()
            self._container_pool = None

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
                        "Guardian blocked memory context from system prompt (confidence=%.3f)",
                        (
                            screen_result.classifier_result.confidence
                            if screen_result.classifier_result
                            else 0.0
                        ),
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
