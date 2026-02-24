"""Job execution — runs cron job payloads in main-session or isolated mode."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from contextlib import contextmanager

from taskrunner.agent import run_agent_loop
from taskrunner.cron.delivery import deliver
from taskrunner.cron.models import ChannelSendFn, CronJob
from taskrunner.secrets import decrypt_env_file

logger = logging.getLogger(__name__)

# Callback to inject a system event message into the main session.
# Receives the formatted event text.
InjectEventFn = Callable[[str], None]

# Serialize secret loading so concurrent isolated jobs don't race on os.environ.
_secrets_lock = threading.Lock()


@contextmanager
def _temporary_secrets(secrets_path: str):
    """Load secrets into os.environ and restore originals on exit.

    Acquires _secrets_lock to prevent concurrent isolated jobs from
    seeing each other's secrets or corrupting os.environ state.
    """
    with _secrets_lock:
        secrets = decrypt_env_file(secrets_path)
        originals: dict[str, str | None] = {}
        for key in secrets:
            originals[key] = os.environ.get(key)
        # Inject
        for key, value in secrets.items():
            os.environ[key] = value
        try:
            yield
        finally:
            # Restore originals or remove injected keys
            for key, original in originals.items():
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original


class JobExecutor:
    """Executes cron job payloads.

    Main-session jobs inject a system event into the conversation.
    Isolated jobs run a fresh agent turn and deliver the output.
    """

    def __init__(
        self,
        agent_def: object | None = None,
        inject_event: InjectEventFn | None = None,
        channel_send: ChannelSendFn | None = None,
        use_containers: bool = False,
    ) -> None:
        self._agent_def = agent_def
        self._inject_event = inject_event
        self._channel_send = channel_send
        self._use_containers = use_containers

    def __call__(self, job: CronJob) -> None:
        """Execute a job based on its target mode."""
        if job.target == "main":
            self._execute_main(job)
        else:
            self._execute_isolated(job)

    def _execute_main(self, job: CronJob) -> None:
        """Inject a system event into the main conversation session."""
        if self._inject_event is None:
            raise RuntimeError(
                "Cannot execute main-session job: no event injector configured"
            )

        event_text = f"[Scheduled: {job.name}]\n{job.payload.message}"
        self._inject_event(event_text)
        logger.info("Injected system event for job '%s' (%s)", job.name, job.id)

    def _execute_isolated(self, job: CronJob) -> None:
        """Run a fresh agent turn in a dedicated session."""
        if self._agent_def is None:
            raise RuntimeError(
                "Cannot execute isolated job: no agent definition configured"
            )

        # Build LLM config, applying model override if specified
        llm_config = self._agent_def.llm.model_copy()
        if job.payload.model:
            llm_config.model = job.payload.model

        messages: list[dict] = [{"role": "user", "content": job.payload.message}]

        def _run() -> object:
            return run_agent_loop(
                messages=messages,
                llm_config=llm_config,
                tools_config=self._agent_def.tools,
                agent_config=self._agent_def.agent,
                use_containers=self._use_containers,
            )

        # Load secrets with cleanup to avoid leaking to concurrent threads
        if llm_config.secrets:
            with _temporary_secrets(llm_config.secrets):
                result = _run()
        else:
            result = _run()

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
