"""Chat server - wires channels, sessions, and the agent loop together."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creel.agent import run_agent_loop
from creel.approvals import ApprovalQueue
from creel.channels.message import Attachment
from creel.commands import ChatContext, SlashCommandRegistry, build_default_registry
from creel.log import generate_request_id, request_id_var
from creel.media import MediaProcessor
from creel.memory import MemoryManager
from creel.models import AgentDefinition, LLMConfig, SessionState
from creel.prompt_builder import build_system_prompt
from creel.quiet_hours import should_suppress
from creel.session import SessionManager
from creel.skills.registry import SkillRegistry
from creel.subagents import SubAgentManager
from creel.tool_cache import ToolResultCache
from creel.tools import execute_tool_call

logger = logging.getLogger(__name__)


def _strip_image_data_from_history(messages: list[dict], *, up_to: int | None = None) -> None:
    """Replace base64 image data in session messages with lightweight placeholders.

    Images are only needed by the LLM for the turn they arrive. Keeping
    the full base64 in history causes every subsequent turn to re-send
    tens of thousands of tokens worth of image data.

    Args:
        messages: The session message list (mutated in-place).
        up_to: Only process messages at indices ``0..up_to-1``.  When
            *None*, all messages are processed.

    Modifies messages in-place. Handles both Anthropic and OpenAI formats.
    """
    target = messages[:up_to] if up_to is not None else messages
    for msg in target:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue

            # Anthropic format: {"type": "image", "source": {"type": "base64", ...}}
            if block.get("type") == "image":
                source = block.get("source")
                if isinstance(source, dict) and source.get("type") == "base64" and "data" in source:
                    data_len = len(source.get("data", ""))
                    media_type = source.get("media_type", "image/unknown")
                    content[i] = {
                        "type": "text",
                        "text": f"[Image ({media_type}, ~{data_len // 1024}KB) — previously analyzed]",
                    }

            # OpenAI format: {"type": "image_url", "image_url": {"url": "data:...;base64,..."}}
            elif block.get("type") == "image_url":
                url = (block.get("image_url") or {}).get("url", "")
                if url.startswith("data:") and ";base64," in url:
                    content[i] = {
                        "type": "text",
                        "text": "[Image — previously analyzed]",
                    }


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
        registry: SkillRegistry | None = None,
    ):
        self._agent_def = agent_def
        self._use_containers = use_containers
        self._cron_manager = cron_manager
        if registry is not None:
            self._registry: SkillRegistry = registry
        else:
            from creel.skills.registry import get_shared_registry

            self._registry = get_shared_registry()
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

        # Build session transcript archival callback.
        # Uses a closure over self so it works even though MemoryManager
        # is constructed after SessionManager.
        on_session_archived = None
        if agent_def.workspace.index_session_transcripts:

            def _archive_transcript(session_id: str, messages: list[dict]) -> None:
                if self._memory is not None:
                    self._memory.index_transcript(session_id, messages)

            on_session_archived = _archive_transcript

        self._session_mgr = SessionManager(
            sessions_dir=agent_def.session.sessions_dir,
            ttl_hours=agent_def.session.ttl_hours,
            summarize_on_trim=agent_def.session.summarize_on_trim,
            summarize_fn=summarize_fn,
            max_context_tokens=agent_def.session.max_context_tokens,
            on_session_archived=on_session_archived,
        )
        self._summarize_fn = summarize_fn

        # Initialize tool result cache from config.
        cache_cfg = agent_def.session.tool_cache
        # Per-tool TTL overrides from session.tool_cache.tool_ttls
        merged_ttls = dict(cache_cfg.tool_ttls)
        self._tool_cache: ToolResultCache | None = None
        if cache_cfg.enabled:
            self._tool_cache = ToolResultCache(
                tool_ttls=merged_ttls,
                default_ttl=cache_cfg.default_ttl,
                max_entries=cache_cfg.max_entries,
            )

        # Build memory compaction summarize callback.
        # NOTE: This callback fires only during __init__ (via compact_daily_files),
        # making N sequential Haiku calls for N old daily files. Acceptable
        # given Haiku latency (~200ms/call) but startup scales linearly with
        # compaction backlog.
        compact_summarize_fn = None
        if agent_def.workspace.compact_summarize:
            # Pre-load secrets once rather than on every callback invocation.
            if agent_def.llm.secrets:
                from creel.orchestrator import _load_secrets_to_env

                _load_secrets_to_env(agent_def.llm.secrets)

            # Capture only the values the closure needs, not the full agent_def.
            _compact_model = agent_def.workspace.compact_model
            _compact_max_tokens = agent_def.workspace.compact_max_tokens
            _llm_secrets = agent_def.llm.secrets
            _use_containers = use_containers

            def _summarize_memory(entries: list[str]) -> str:
                from creel.llm import run_llm

                entries_text = "\n".join(f"- {e}" for e in entries)
                prompt = (
                    "You are summarizing a day's memory entries for a personal AI assistant. "
                    "Extract the key facts, decisions, and context into 3-7 concise bullet "
                    "points. Preserve specific names, dates, numbers, and action items. "
                    "Drop routine/trivial entries. Output only markdown bullets (- item)."
                    f"\n\nEntries:\n{entries_text}"
                )

                config = LLMConfig(
                    model=_compact_model,
                    max_tokens=_compact_max_tokens,
                    secrets=_llm_secrets,
                )
                return run_llm(prompt, config, use_container=_use_containers)

            compact_summarize_fn = _summarize_memory

        # Initialize memory manager if workspace is configured
        self._memory: MemoryManager | None = None
        ws_path = Path(agent_def.workspace.path)
        if ws_path.is_dir():
            self._memory = MemoryManager(
                workspace_dir=agent_def.workspace.path,
                timezone_name=agent_def.workspace.timezone,
                max_daily_entries=agent_def.workspace.max_daily_entries,
                max_long_term_lines=agent_def.workspace.max_long_term_lines,
                fts_enabled=agent_def.workspace.fts_enabled,
                recency_half_life_days=agent_def.workspace.recency_half_life_days,
                compact_summarize_fn=compact_summarize_fn,
                extra_paths=agent_def.workspace.extra_paths,
            )
            self._memory.rebuild_index()
            self._memory.compact_daily_files(
                days_to_keep=agent_def.workspace.compact_after_days,
                summarize=agent_def.workspace.compact_summarize,
            )
            logger.info("Memory system enabled (workspace: %s)", agent_def.workspace.path)

        # Initialize knowledge base if configured and enabled
        self._kb: Any = None
        kb_config = agent_def.knowledge_base
        if kb_config.enabled:
            from creel.knowledge_base import KnowledgeBase

            kb_db_path = kb_config.db_path or str(ws_path / ".kb_index.sqlite")
            try:
                self._kb = KnowledgeBase(
                    db_path=kb_db_path,
                    chunk_size=kb_config.chunk_size,
                    chunk_overlap=kb_config.chunk_overlap,
                    embedding_model=kb_config.embedding_model,
                )
                # Auto-index configured directories
                if kb_config.auto_index:
                    self._kb.reindex_auto_paths(kb_config.auto_index)
                logger.info("Knowledge base enabled (db: %s)", kb_db_path)
            except (sqlite3.Error, OSError):
                logger.error("Failed to initialize knowledge base", exc_info=True)

        # Per-sender session state (e.g. workspace path for file_ops)
        # Default workspace to the agent config workspace path so file_ops
        # tools work without the LLM explicitly calling set_workspace first.
        self._default_workspace: str | None = str(ws_path.resolve()) if ws_path.is_dir() else None
        self._session_states: dict[str, SessionState] = {}

        # Track active agent loops per sender for interrupt word detection.
        self._active_loops: dict[str, SessionState] = {}
        self._active_loops_lock = threading.Lock()
        self._interrupt_words: frozenset[str] = frozenset(
            w.lower() for w in agent_def.agent.interrupt_words
        )

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

        # Initialize media processor for attachment handling
        media_cfg = agent_def.media
        self._media: MediaProcessor | None = None
        if media_cfg is not None and media_cfg.enabled:
            self._media = MediaProcessor(media_cfg)

        # Expose individual media services for direct access
        self._media_store = self._media._store if self._media else None
        self._transcription = self._media._transcription if self._media else None
        self._vision = self._media._vision if self._media else None

        # Initialize sub-agent manager
        self._subagent_manager = SubAgentManager(
            llm_config=agent_def.llm,
            agent_config=agent_def.agent,
            skill_overrides=agent_def.skills,
            system_prompt=None,  # built lazily per request
            use_containers=use_containers,
            guardian=self._guardian,
            bridge_config=agent_def.bridge,
            result_callback=self._on_subagent_result,
            safety_config=agent_def.safety,
        )

        # Initialize slash command registry
        self._command_registry = build_default_registry()

    @property
    def command_registry(self) -> SlashCommandRegistry:
        """Return the slash command registry (for plugin registration)."""
        return self._command_registry

    def update_agent_def(self, agent_def: AgentDefinition) -> None:
        """Swap the agent definition and update derived references.

        Called by DaemonService during hot-reload. Components that cache
        config at init time (SessionManager, MemoryManager, ContainerPool)
        are intentionally left untouched — their settings are either
        non-reloadable or don't benefit from live swaps.
        """
        self._agent_def = agent_def
        self._interrupt_words = frozenset(w.lower() for w in agent_def.agent.interrupt_words)
        # SubAgentManager holds config refs used when spawning new agents.
        self._subagent_manager._llm_config = agent_def.llm
        self._subagent_manager._skill_overrides = agent_def.skills
        self._subagent_manager._agent_config = agent_def.agent
        self._subagent_manager._bridge_config = agent_def.bridge

    def interrupt_sender(self, sender_id: str) -> bool:
        """Set the interrupt signal for a sender's active agent loop.

        Thread-safe — uses a lightweight lock, not the main service lock.
        Returns True if an active loop was found and signalled.
        """
        with self._active_loops_lock:
            active_state = self._active_loops.get(sender_id)
        if active_state is not None:
            active_state.interrupt.set()
            logger.info("Interrupt signal sent for %s", sender_id)
            return True
        return False

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

        # Check if this is an interrupt word for an active agent loop.
        # The interrupt check here is the authoritative layer; DaemonService
        # also checks before its lock as a fast-path so interrupt words can
        # signal a running loop without blocking.
        if stripped.lower() in self._interrupt_words:
            if self.interrupt_sender(sender_id):
                return "Stopping..."

        # Legacy bare-word aliases (backward compat: "clear" and "reset" without /)
        if stripped.lower() in {"clear", "reset"}:
            stripped = f"/{stripped}"

        # Screen slash command input through Guardian before dispatch.
        # Commands like /allow modify security policy — their arguments
        # must be screened for prompt injection.
        # However, built-in security commands (/allow, /allows, /deny, /help,
        # /new, /status) are exempt since they come from the authenticated user
        # and blocking them defeats their purpose.
        _GUARDIAN_EXEMPT_COMMANDS = frozenset(
            {
                "allow",
                "allows",
                "deny",
                "help",
                "new",
                "status",
                "sessions",
                "resume",
                "model",
                "tools",
                "memory",
            }
        )
        if stripped.startswith("/") and self._guardian:
            cmd_name = stripped.lstrip("/").split()[0].lower()
            if cmd_name not in _GUARDIAN_EXEMPT_COMMANDS:
                screen_result = self._guardian.screen_input(text)
                if screen_result.blocked:
                    logger.warning("Guardian blocked slash command input from %s", sender_id)
                    return screen_result.rejection_message

        # Dispatch slash commands via registry
        if stripped.startswith("/"):
            ctx = ChatContext(sender_id=sender_id, server=self)
            result = self._command_registry.handle(stripped, ctx)
            if result is not None:
                return result

        # Check for pending approval response BEFORE normal processing
        if stripped.lower() in _APPROVE_WORDS | _DENY_WORDS:
            pending_actions = self._approval_queue.get_all_pending(sender_id)
            if pending_actions:
                return self._handle_approval_response(
                    sender_id,
                    pending_actions,
                    stripped.lower() in _APPROVE_WORDS,
                )

        # Screen input through guardian (before adding to session)
        if self._guardian:
            screen_result = self._guardian.screen_input(text)
            if screen_result.blocked:
                logger.warning("Guardian blocked input from %s", sender_id)
                return screen_result.rejection_message

        # Do not start a new LLM turn while an approval is pending.
        pending_actions = self._approval_queue.get_all_pending(sender_id)
        if pending_actions:
            tool_names = ", ".join(f"`{a.tool_name}`" for a in pending_actions)
            return f"⏳ Approval still pending for {tool_names}. Reply Y to approve or N to deny."

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
                if reason.startswith("[BLOCKLIST]"):
                    logger.warning("Blocklist match cannot be auto-approved: %s", reason)
                    return False
                logger.info("Auto-approving %s (reason: %s)", tool_name, reason)
                if self._guardian is not None:
                    self._guardian.log_action_outcome(tool_name, "review", "auto_approved_by_cli")
                return True

            confirm_action = _auto_confirm

        # Look up per-sender session state (workspace path, etc.)
        session_state = self._session_states.setdefault(
            sender_id,
            SessionState(sender_id=sender_id, workspace=self._default_workspace),
        )

        # Clear any stale interrupt signal and register as active
        session_state.interrupt.clear()
        with self._active_loops_lock:
            self._active_loops[sender_id] = session_state

        # Record message count before the agent loop so we can strip
        # image data only from prior turns (not the current one).
        # Subtract 1 to exclude the user message just added above —
        # its images need to survive this turn.
        _pre_loop_msg_count = max(0, len(session.messages) - 1)

        # Run the agent loop (containerized or direct)
        try:
            result = self._invoke_agent_loop(
                session.messages,
                session_state,
                user_message=text,
                confirm_action=confirm_action,
                on_text_delta=on_text_delta,
            )
        finally:
            with self._active_loops_lock:
                self._active_loops.pop(sender_id, None)
            session_state.interrupt.clear()

        logger.info(
            "Agent response for %s: %d chars, %d turns, %d tool calls (%s)",
            sender_id,
            len(result.text),
            result.turns_used,
            result.tool_calls_made,
            result.stop_reason,
        )

        # Track token usage for session metadata
        last_tokens = getattr(result, "last_input_tokens", 0)
        if isinstance(last_tokens, int) and last_tokens > 0:
            self._session_mgr.update_token_count(sender_id, last_tokens)

        # Handle approval_required — queue all pending actions and notify
        if result.stop_reason == "approval_required" and result.pending_approvals:
            for pa in result.pending_approvals:
                self._approval_queue.add(
                    sender_id=sender_id,
                    tool_name=pa.tool_name,
                    tool_input=pa.tool_input,
                    reason=pa.reason,
                    tool_use_id=pa.tool_use_id,
                )
            self._send_batch_approval_request(sender_id, result.pending_approvals)
            self._session_mgr.save_session(session)
            return "⏳ Waiting for your approval to proceed."

        # Strip base64 image data from prior turns to prevent
        # re-sending large images on every subsequent turn.  The current
        # turn's images are kept so the LLM saw them and so that any
        # post-processing / test assertions still pass.
        _strip_image_data_from_history(session.messages, up_to=_pre_loop_msg_count)

        # Save the updated messages (agent loop mutates the list)
        self._session_mgr.save_session(session)

        # When interrupted, the channel already sent "Stopping..." to the user.
        # Return empty string so the worker thread doesn't send a duplicate reply.
        if result.stop_reason == "interrupted":
            return ""

        return result.text

    def _process_attachments(
        self,
        text: str,
        attachments: list[Attachment] | None,
        sender_id: str,
        channel: str = "unknown",
    ) -> tuple[str, list[dict]]:
        """Process media attachments via the MediaProcessor.

        Returns:
            (updated_text, image_content_blocks)
        """
        if not attachments:
            return text, []
        if self._media is None:
            logger.info("Media disabled — ignoring %d attachment(s)", len(attachments))
            return text, []
        return self._media.process_attachments(text, attachments, sender_id, channel)

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

    def _send_batch_approval_request(self, sender_id: str, pending_approvals) -> None:
        """Send a batch approval request listing all tools that need approval."""
        if should_suppress(self._agent_def.quiet_hours, urgent=False):
            logger.info("Approval request suppressed due to quiet hours")
            return

        tool_sections = []
        for pa in pending_approvals:
            args_lines = []
            tool_input = pa.tool_input if hasattr(pa, "tool_input") else {}
            for k, v in tool_input.items():
                v_str = str(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
                args_lines.append(f"    {k}: {v_str}")
            args_summary = "\n".join(args_lines) if args_lines else "    (none)"
            tool_name = pa.tool_name if hasattr(pa, "tool_name") else pa.tool_name
            reason = pa.reason if hasattr(pa, "reason") else ""
            tool_sections.append(f"  🔧 {tool_name}\n{args_summary}\n  📝 {reason}")

        tools_list = "\n\n".join(tool_sections)
        count = len(pending_approvals)
        msg = (
            f"⚠️ {count} action{'s' if count > 1 else ''} require approval:\n\n"
            f"{tools_list}\n\n"
            f"Reply **Y** to approve all or **N** to deny all."
        )

        if self._reply_channel:
            try:
                self._reply_channel.send(sender_id, msg)
            except Exception:
                logger.exception("Failed to send approval request via channel")
        else:
            logger.info("Approval request (no reply channel): %s", msg)

    def _invoke_agent_loop(
        self,
        messages: list[dict],
        session_state: SessionState,
        *,
        user_message: str | None = None,
        confirm_action: Callable[[str, dict, str], bool] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ):
        """Build system prompt, load secrets, and run the agent loop.

        Centralises the "prepare + invoke" sequence so handle_message and
        _handle_approval_response stay in sync.
        """
        system_prompt = self._build_system_prompt(user_message=user_message)

        if self._agent_def.llm.secrets:
            from creel.orchestrator import _load_secrets_to_env

            _load_secrets_to_env(self._agent_def.llm.secrets)

        if self._use_containers:
            from creel.container_agent import run_agent_loop_container

            return run_agent_loop_container(
                messages=messages,
                llm_config=self._agent_def.llm,
                agent_config=self._agent_def.agent,
                system_prompt=system_prompt,
                use_containers=self._use_containers,
                guardian=self._guardian,
                confirm_action=confirm_action,
                memory_manager=self._memory,
                bridge_config=self._agent_def.bridge,
                session_state=session_state,
                container_pool=self._container_pool,
                safety_config=self._agent_def.safety,
                registry=self._registry,
                skill_overrides=self._agent_def.skills,
            )

        return run_agent_loop(
            messages=messages,
            llm_config=self._agent_def.llm,
            agent_config=self._agent_def.agent,
            registry=self._registry,
            skill_overrides=self._agent_def.skills,
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
            kb_manager=self._kb,
            tool_cache=self._tool_cache,
            context_pruning=self._agent_def.session.context_pruning,
            max_context_tokens=self._agent_def.session.max_context_tokens,
            summarize_fn=self._summarize_fn,
            safety_config=self._agent_def.safety,
        )

    def _handle_approval_response(
        self,
        sender_id: str,
        pending_actions: list,
        approved: bool,
    ) -> str:
        """Resolve all pending actions, execute if approved, and resume the agent loop."""
        for pa in pending_actions:
            self._approval_queue.resolve(pa.id, approved)

        session = self._session_mgr.get_or_create(sender_id)

        if not approved:
            denied_names = []
            for pa in pending_actions:
                if pa.tool_use_id:
                    if not self._patch_tool_result(
                        session.messages,
                        pa.tool_use_id,
                        f"Action denied by user: {pa.policy_reason}",
                        is_error=True,
                    ):
                        logger.warning(
                            "Could not patch tool_result for %s (tool_use_id=%s)",
                            pa.tool_name,
                            pa.tool_use_id,
                        )
                denied_names.append(pa.tool_name)
            self._session_mgr.save_session(session)
            return f"❌ Actions denied: {', '.join(denied_names)}"

        # Execute all approved tools and patch their results
        session_state = self._session_states.setdefault(
            sender_id,
            SessionState(sender_id=sender_id, workspace=self._default_workspace),
        )
        for pa in pending_actions:
            is_error = False
            try:
                tool_result = execute_tool_call(
                    tool_name=pa.tool_name,
                    tool_input=pa.tool_input,
                    registry=self._registry,
                    skill_overrides=self._agent_def.skills,
                    use_containers=self._use_containers,
                    memory_manager=self._memory,
                    bridge_config=self._agent_def.bridge,
                    session_state=session_state,
                    cron_manager=self._cron_manager,
                    subagent_manager=self._subagent_manager,
                )
            except Exception as e:
                logger.exception("Tool execution failed after approval: %s", pa.tool_name)
                tool_result = f"Error: {e}"
                is_error = True

            if pa.tool_use_id:
                if not self._patch_tool_result(
                    session.messages, pa.tool_use_id, tool_result, is_error
                ):
                    logger.warning(
                        "Could not patch tool_result for %s (tool_use_id=%s)",
                        pa.tool_name,
                        pa.tool_use_id,
                    )

        # Resume the agent loop so the LLM can process the tool outputs
        session_state.interrupt.clear()
        with self._active_loops_lock:
            self._active_loops[sender_id] = session_state
        try:
            result = self._invoke_agent_loop(session.messages, session_state)
        finally:
            with self._active_loops_lock:
                self._active_loops.pop(sender_id, None)
            session_state.interrupt.clear()

        # Track token usage for session metadata
        last_tokens = getattr(result, "last_input_tokens", 0)
        if isinstance(last_tokens, int) and last_tokens > 0:
            self._session_mgr.update_token_count(sender_id, last_tokens)

        # If the resumed loop itself hits another approval_required, queue it
        if result.stop_reason == "approval_required" and result.pending_approvals:
            for pa in result.pending_approvals:
                self._approval_queue.add(
                    sender_id=sender_id,
                    tool_name=pa.tool_name,
                    tool_input=pa.tool_input,
                    reason=pa.reason,
                    tool_use_id=pa.tool_use_id,
                )
            self._send_batch_approval_request(sender_id, result.pending_approvals)
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

    def _handle_compact(self, sender_id: str) -> str:
        """Handle the /compact command — summarize older context."""
        session = self._session_mgr.get_or_create(sender_id)
        before = len(session.messages)
        if before <= 2:
            return "Nothing to compact — session is too short."
        self._session_mgr.compact(sender_id)
        session = self._session_mgr.get_or_create(sender_id)
        after = len(session.messages)
        return f"Compacted {before} messages → {after} (summary + recent)."

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

    def _handle_allow(self, sender_id: str, text: str) -> str:
        """Handle /allow <pattern> [<count>x] [<duration>] — create a temporary allow override."""
        if not self._guardian:
            return "Guardian is not enabled."

        from guardian.overrides import parse_duration, parse_use_count
        from guardian.types import ActionVerdict

        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return "Usage: /allow <pattern> [<count>x] [<duration>]"

        args = parts[1].strip()
        tokens = args.split()
        pattern = tokens[0]
        remaining = " ".join(tokens[1:])

        # Check for bare wildcard without confirmation
        override_mgr = self._guardian.override_manager
        if pattern == "*" and override_mgr.requires_wildcard_confirmation:
            if not remaining.endswith("confirm"):
                return (
                    "Warning: `/allow *` matches ALL tools. "
                    "Append `confirm` to proceed, e.g. `/allow * 30m confirm`."
                )
            remaining = remaining.rsplit("confirm", 1)[0].strip()

        # Parse use count and duration from remaining args
        max_uses, remaining = parse_use_count(remaining)
        duration_seconds = 30 * 60  # default 30m
        if remaining:
            try:
                duration_seconds = parse_duration(remaining)
            except ValueError as e:
                return f"Invalid duration: {e}"

        try:
            override = override_mgr.create_override(
                pattern=pattern,
                action=ActionVerdict.ALLOW,
                duration_seconds=duration_seconds,
                created_by=sender_id,
                max_uses=max_uses,
            )
        except ValueError as e:
            return f"Cannot create override: {e}"

        expiry = override.expires_at.strftime("%H:%M:%S UTC")
        uses_str = f", max {max_uses} uses" if max_uses else ""
        return f"Allowing `{pattern}` until {expiry}{uses_str}."

    def _handle_deny(self, sender_id: str, text: str) -> str:
        """Handle /deny <pattern> — revoke an active allow override."""
        if not self._guardian:
            return "Guardian is not enabled."

        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return "Usage: /deny <pattern>"

        pattern = parts[1].strip()
        override_mgr = self._guardian.override_manager
        revoked = override_mgr.revoke_override(pattern)
        if revoked:
            return f"Revoked override for `{pattern}`."
        return f"No active override found for `{pattern}`."

    def _handle_allows(self, sender_id: str) -> str:
        """Handle /allows — list all active temporary overrides."""
        if not self._guardian:
            return "Guardian is not enabled."

        override_mgr = self._guardian.override_manager
        active = override_mgr.list_active()
        if not active:
            return "No active overrides."

        lines = ["Active overrides:", ""]
        for ov in active:
            uses_str = f"{ov.use_count}/{ov.max_uses}" if ov.max_uses else f"{ov.use_count}/∞"
            remaining = ov.remaining_seconds
            if remaining >= 3600:
                time_str = f"{remaining // 3600}h{(remaining % 3600) // 60}m"
            elif remaining >= 60:
                time_str = f"{remaining // 60}m{remaining % 60}s"
            else:
                time_str = f"{remaining}s"
            lines.append(
                f"  {ov.action.value:5s}  {ov.pattern:<20s}  uses={uses_str}  expires in {time_str}"
            )
        return "\n".join(lines)

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

        from creel.dev_session_manager import shutdown_dev_session_manager

        shutdown_dev_session_manager()

    def _build_system_prompt(self, *, user_message: str | None = None) -> str:
        """Build the system prompt from workspace files, memory, and config.

        This mirrors OpenClaw's pattern of assembling the system prompt from
        multiple sources each run, rather than using a static string.

        Args:
            user_message: Latest user message, used for relevant-mode context
                injection to select only memories that match the query.
        """
        ws_cfg = self._agent_def.workspace

        # Load base prompt from file if configured, else use inline
        base_prompt = self._agent_def.system_prompt
        if self._agent_def.system_prompt_file:
            prompt_path = Path(self._agent_def.system_prompt_file)
            if prompt_path.exists():
                base_prompt = prompt_path.read_text().strip()

        # Get memory context — "recent" dumps last N days, "relevant" uses search
        memory_context = None
        if self._memory:
            if ws_cfg.memory_context_mode == "relevant" and user_message:
                memory_context = self._memory.get_relevant_context(
                    query=user_message,
                    max_results=ws_cfg.memory_context_max_results,
                    max_chars=ws_cfg.memory_max_chars,
                )
            else:
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
            registry=self._registry,
            memory_context=memory_context,
            max_chars_per_file=ws_cfg.max_chars_per_file,
        )
