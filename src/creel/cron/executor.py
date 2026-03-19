"""Job execution — runs cron job payloads in main-session or isolated mode."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from creel.agent import run_agent_loop
from creel.cron.delivery import deliver
from creel.cron.models import ChannelSendFn, CronJob

if TYPE_CHECKING:
    from creel.models import AgentDefinition

logger = logging.getLogger(__name__)

# Callback to inject a system event message into the main session.
# Receives the formatted event text.
InjectEventFn = Callable[[str], None]


class JobExecutor:
    """Executes cron job payloads.

    Main-session jobs inject a system event into the conversation.
    Isolated jobs run a fresh agent turn and deliver the output.
    """

    def __init__(
        self,
        agent_def: AgentDefinition | None = None,
        inject_event: InjectEventFn | None = None,
        channel_send: ChannelSendFn | None = None,
        use_containers: bool = False,
    ) -> None:
        self._agent_def = agent_def
        self._inject_event = inject_event
        self._channel_send = channel_send
        self._use_containers = use_containers

    def update_agent_def(self, agent_def: AgentDefinition) -> None:
        """Swap the agent definition reference for hot-reload."""
        self._agent_def = agent_def

    def __call__(self, job: CronJob) -> None:
        """Execute a job based on its target mode."""
        if job.target == "main":
            self._execute_main(job)
        else:
            self._execute_isolated(job)

    def _execute_main(self, job: CronJob) -> None:
        """Inject a system event into the main conversation session."""
        if self._inject_event is None:
            raise RuntimeError("Cannot execute main-session job: no event injector configured")

        event_text = f"[Scheduled: {job.name}]\n{job.payload.message}"
        self._inject_event(event_text)
        logger.info("Injected system event for job '%s' (%s)", job.name, job.id)

    def _execute_isolated(self, job: CronJob) -> None:
        """Run a fresh agent turn in a dedicated session."""
        if self._agent_def is None:
            raise RuntimeError("Cannot execute isolated job: no agent definition configured")

        # Build LLM config, applying model override if specified
        llm_config = self._agent_def.llm.model_copy()
        if job.payload.model:
            llm_config.model = job.payload.model

        messages: list[dict] = [{"role": "user", "content": job.payload.message}]

        from creel.skills.registry import get_shared_registry

        _registry = get_shared_registry()

        result = run_agent_loop(
            messages=messages,
            llm_config=llm_config,
            agent_config=self._agent_def.agent,
            registry=_registry,
            skill_overrides=self._agent_def.skills,
            use_containers=self._use_containers,
        )

        logger.info(
            "Isolated job '%s' (%s) completed: %d turns, %d tool calls, stop=%s",
            job.name,
            job.id,
            result.turns_used,
            result.tool_calls_made,
            result.stop_reason,
        )

        # Deliver the output
        deliver(
            delivery=job.delivery,
            output=result.text,
            job=job,
            channel_send=self._channel_send,
        )
