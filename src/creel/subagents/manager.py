"""SubAgentManager — spawns and manages isolated child agent loops."""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from creel.models import SessionState
from creel.subagents.models import SubAgentConfig, SubAgentInfo, SubAgentStatus

logger = logging.getLogger(__name__)


class SubAgentManager:
    """Manages spawning, tracking, and lifecycle of sub-agent runs.

    Each sub-agent runs an isolated agent loop in a daemon thread with its own
    session (message history).  On completion the result is injected back into
    the parent session via a callback.

    Current limitations:
    - Sub-agents do not have access to memory tools or cron tools.
    - Nested sub-agent spawning is not supported (no subagent_manager passed).
    - Each sub-agent gets an empty session_state — it cannot read/write the
      parent's workspace or other per-session data.
    """

    def __init__(
        self,
        *,
        llm_config: Any,
        agent_config: Any,
        skill_overrides: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        use_containers: bool = False,
        guardian: Any | None = None,
        bridge_config: Any | None = None,
        result_callback: Callable[[str, str], None] | None = None,
        safety_config: Any | None = None,
    ) -> None:
        self._llm_config = llm_config
        self._skill_overrides = skill_overrides or {}
        self._agent_config = agent_config
        self._system_prompt = system_prompt
        self._use_containers = use_containers
        self._guardian = guardian
        self._bridge_config = bridge_config
        self._safety_config = safety_config
        self._result_callback = result_callback

        # agent_id → SubAgentInfo
        self._agents: dict[str, SubAgentInfo] = {}
        # agent_id → threading.Thread
        self._threads: dict[str, threading.Thread] = {}
        # agent_id → cancel Event (set to signal the loop should stop)
        self._cancel_events: dict[str, threading.Event] = {}
        # agent_id → list of messages to inject on the next iteration
        self._steer_queues: dict[str, list[str]] = {}
        # Protects _agents / _threads / _cancel_events / _steer_queues
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def spawn(self, config: SubAgentConfig, *, sender_id: str = "") -> str:
        """Spawn a new sub-agent and return its ID immediately."""
        agent_id = secrets.token_hex(4)
        label = config.label or f"subagent-{agent_id}"
        cancel_event = threading.Event()

        info = SubAgentInfo(
            id=agent_id,
            label=label,
            status=SubAgentStatus.RUNNING,
            sender_id=sender_id,
        )

        with self._lock:
            self._agents[agent_id] = info
            self._cancel_events[agent_id] = cancel_event
            self._steer_queues[agent_id] = []

        thread = threading.Thread(
            target=self._run_agent,
            args=(agent_id, config, cancel_event),
            daemon=True,
            name=f"subagent-{agent_id}",
        )
        with self._lock:
            self._threads[agent_id] = thread
        thread.start()

        # Timeout watchdog
        if config.timeout_seconds:
            timer = threading.Timer(
                config.timeout_seconds,
                self._handle_timeout,
                args=(agent_id,),
            )
            timer.daemon = True
            timer.start()

        logger.info("Spawned sub-agent %s (%s): %s", agent_id, label, config.task[:80])
        return agent_id

    def list_agents(self) -> list[SubAgentInfo]:
        """Return status of all sub-agents (newest first)."""
        with self._lock:
            return sorted(
                self._agents.values(),
                key=lambda a: a.started_at,
                reverse=True,
            )

    def steer(self, agent_id: str, message: str) -> bool:
        """Inject a follow-up user message into a running sub-agent.

        Returns True if the message was queued, False if the agent is not running.
        """
        with self._lock:
            info = self._agents.get(agent_id)
            if info is None or info.status != SubAgentStatus.RUNNING:
                return False
            self._steer_queues[agent_id].append(message)
        return True

    def kill(self, agent_id: str) -> bool:
        """Terminate a running sub-agent.

        Returns True if the kill signal was sent, False if not running.
        """
        with self._lock:
            info = self._agents.get(agent_id)
            if info is None or info.status != SubAgentStatus.RUNNING:
                return False
            cancel = self._cancel_events.get(agent_id)

        if cancel:
            cancel.set()

        # Update status (the thread will also set it, but we set here for
        # immediate feedback).
        with self._lock:
            info = self._agents.get(agent_id)
            if info and info.status == SubAgentStatus.RUNNING:
                info.status = SubAgentStatus.KILLED
                info.completed_at = datetime.now(UTC)
                info.error = "Killed by parent"

        logger.info("Kill signal sent to sub-agent %s", agent_id)
        return True

    def get(self, agent_id: str) -> SubAgentInfo | None:
        """Look up a sub-agent by ID."""
        with self._lock:
            return self._agents.get(agent_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_agent(
        self,
        agent_id: str,
        config: SubAgentConfig,
        cancel: threading.Event,
    ) -> None:
        """Run the agent loop in a background thread."""
        from creel.agent import AgentResult, run_agent_loop
        from creel.models import AgentConfig, LLMConfig

        # Build isolated message history starting with the task
        messages: list[dict] = [{"role": "user", "content": config.task}]

        # Optionally override the model
        llm_config = self._llm_config
        if config.model:
            llm_config = LLMConfig(
                model=config.model,
                max_tokens=self._llm_config.max_tokens,
                secrets=self._llm_config.secrets,
            )

        # Use a modest max_turns for sub-agents to bound cost
        agent_cfg = AgentConfig(max_turns=min(self._agent_config.max_turns, 25))

        result: AgentResult | None = None
        try:
            # Run the agent loop; it blocks until the agent finishes or
            # hits max_turns.  We check the cancel event after each call.
            # The agent loop itself may take many turns, so we rely on
            # the timeout watchdog + the thread daemon flag for safety.
            if cancel.is_set():
                return

            from creel.skills.registry import get_shared_registry

            _registry = get_shared_registry()

            result = run_agent_loop(
                messages=messages,
                llm_config=llm_config,
                agent_config=agent_cfg,
                registry=_registry,
                skill_overrides=self._skill_overrides,
                system_prompt=self._system_prompt,
                use_containers=self._use_containers,
                guardian=self._guardian,
                bridge_config=self._bridge_config,
                session_state=SessionState(),
                safety_config=self._safety_config,
            )

            # Process any steer messages that arrived while the loop ran
            while True:
                with self._lock:
                    queue = self._steer_queues.get(agent_id, [])
                    if not queue or cancel.is_set():
                        break
                    steer_msg = queue.pop(0)

                messages.append({"role": "user", "content": steer_msg})
                result = run_agent_loop(
                    messages=messages,
                    llm_config=llm_config,
                    agent_config=agent_cfg,
                    registry=_registry,
                    skill_overrides=self._skill_overrides,
                    system_prompt=self._system_prompt,
                    use_containers=self._use_containers,
                    guardian=self._guardian,
                    bridge_config=self._bridge_config,
                    session_state=SessionState(),
                    safety_config=self._safety_config,
                )

            if cancel.is_set():
                return  # status already set by kill/timeout

            summary = result.text if result else ""
            # Truncate for storage
            if len(summary) > 2000:
                summary = summary[:2000] + "..."

            with self._lock:
                info = self._agents.get(agent_id)
                if info and info.status == SubAgentStatus.RUNNING:
                    info.status = SubAgentStatus.COMPLETED
                    info.completed_at = datetime.now(UTC)
                    info.result_summary = summary

            logger.info("Sub-agent %s completed", agent_id)
            self._fire_callback(agent_id, summary)

        except Exception as exc:
            logger.exception("Sub-agent %s failed", agent_id)
            with self._lock:
                info = self._agents.get(agent_id)
                if info and info.status == SubAgentStatus.RUNNING:
                    info.status = SubAgentStatus.FAILED
                    info.completed_at = datetime.now(UTC)
                    info.error = str(exc)
            self._fire_callback(agent_id, f"Error: {exc}")

    def _handle_timeout(self, agent_id: str) -> None:
        """Called by the Timer when a sub-agent exceeds its timeout."""
        with self._lock:
            info = self._agents.get(agent_id)
            if info is None or info.status != SubAgentStatus.RUNNING:
                return  # already finished
            cancel = self._cancel_events.get(agent_id)
            info.status = SubAgentStatus.TIMEOUT
            info.completed_at = datetime.now(UTC)
            info.error = "Timed out"

        if cancel:
            cancel.set()

        logger.warning("Sub-agent %s timed out", agent_id)
        self._fire_callback(agent_id, "Timed out")

    def _fire_callback(self, agent_id: str, result_text: str) -> None:
        """Invoke the result callback if configured."""
        if self._result_callback is None:
            return
        with self._lock:
            info = self._agents.get(agent_id)
        label = info.label if info else agent_id
        try:
            self._result_callback(agent_id, result_text)
        except Exception:
            logger.exception("Result callback failed for sub-agent %s (%s)", agent_id, label)
