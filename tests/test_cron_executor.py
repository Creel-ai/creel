"""Tests for cron job executor — main-session and isolated execution modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from creel.cron.executor import JobExecutor
from creel.cron.models import CronJob, Delivery, Payload, Schedule

# -- Helpers --


def _make_job(
    name: str = "test job",
    target: str = "isolated",
    model: str | None = None,
    delivery_mode: str = "none",
    channel: str | None = None,
    url: str | None = None,
) -> CronJob:
    delivery_kwargs: dict = {"mode": delivery_mode}
    if channel:
        delivery_kwargs["channel"] = channel
    if url:
        delivery_kwargs["url"] = url

    return CronJob(
        name=name,
        schedule=Schedule(kind="cron", expr="0 8 * * *"),
        target=target,
        payload=Payload(message="do the thing", model=model),
        delivery=Delivery(**delivery_kwargs),
    )


@dataclass
class FakeAgentResult:
    """Minimal stand-in for AgentResult."""

    text: str = "Agent response"
    turns_used: int = 1
    tool_calls_made: int = 0
    stop_reason: str = "end_turn"
    tool_history: list = field(default_factory=list)


class FakeLLMConfig:
    """Minimal stand-in for LLMConfig with model_copy support."""

    def __init__(self, model: str = "claude-sonnet-4-6", secrets: str | None = None):
        self.model = model
        self.secrets = secrets

    def model_copy(self):
        return FakeLLMConfig(model=self.model, secrets=self.secrets)


class FakeAgentConfig:
    """Minimal stand-in for AgentConfig."""

    def __init__(self, max_turns: int = 10):
        self.max_turns = max_turns

    def model_copy(self):
        return FakeAgentConfig(max_turns=self.max_turns)


class FakeAgentDef:
    """Minimal stand-in for AgentDefinition."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        secrets: str | None = None,
    ):
        self.llm = FakeLLMConfig(model=model, secrets=secrets)
        self.tools = {}
        self.agent = FakeAgentConfig()


# -- Main session execution --


class TestExecuteMain:
    def test_main_session_calls_inject_event(self):
        """Main-session jobs should call inject_event with the formatted message."""
        inject_event = MagicMock()
        executor = JobExecutor(inject_event=inject_event)

        job = _make_job(name="morning reminder", target="main")
        executor(job)

        inject_event.assert_called_once()
        call_arg = inject_event.call_args[0][0]
        assert "[Scheduled: morning reminder]" in call_arg
        assert "do the thing" in call_arg

    def test_main_session_without_injector_raises(self):
        """Main-session execution without an injector should raise RuntimeError."""
        executor = JobExecutor()

        job = _make_job(target="main")
        with pytest.raises(RuntimeError, match="no event injector"):
            executor(job)

    def test_main_session_formats_event_correctly(self):
        """The injected event should include the job name and payload message."""
        inject_event = MagicMock()
        executor = JobExecutor(inject_event=inject_event)

        job = _make_job(name="check email", target="main")
        job.payload.message = "Check for urgent emails"
        executor(job)

        event_text = inject_event.call_args[0][0]
        assert event_text == "[Scheduled: check email]\nCheck for urgent emails"


# -- Isolated execution --


class TestExecuteIsolated:
    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_calls_agent_loop(self, mock_agent_loop):
        """Isolated jobs should call run_agent_loop with the payload message."""
        mock_agent_loop.return_value = FakeAgentResult()
        agent_def = FakeAgentDef()
        executor = JobExecutor(agent_def=agent_def)

        job = _make_job(target="isolated")
        executor(job)

        mock_agent_loop.assert_called_once()
        call_kwargs = mock_agent_loop.call_args[1]
        messages = call_kwargs["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "do the thing"

    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_uses_agent_def_config(self, mock_agent_loop):
        """Isolated jobs should use the agent definition's LLM and tool configs."""
        mock_agent_loop.return_value = FakeAgentResult()
        agent_def = FakeAgentDef(model="claude-sonnet-4-6")
        executor = JobExecutor(agent_def=agent_def)

        job = _make_job(target="isolated")
        executor(job)

        call_kwargs = mock_agent_loop.call_args[1]
        assert call_kwargs["llm_config"].model == "claude-sonnet-4-6"
        assert call_kwargs["tools_config"] == {}

    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_model_override(self, mock_agent_loop):
        """When payload.model is set, it should override the default model."""
        mock_agent_loop.return_value = FakeAgentResult()
        agent_def = FakeAgentDef(model="claude-sonnet-4-6")
        executor = JobExecutor(agent_def=agent_def)

        job = _make_job(target="isolated", model="claude-haiku-4-5")
        executor(job)

        call_kwargs = mock_agent_loop.call_args[1]
        assert call_kwargs["llm_config"].model == "claude-haiku-4-5"

    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_no_model_override_uses_default(self, mock_agent_loop):
        """When payload.model is None, the default model should be used."""
        mock_agent_loop.return_value = FakeAgentResult()
        agent_def = FakeAgentDef(model="claude-sonnet-4-6")
        executor = JobExecutor(agent_def=agent_def)

        job = _make_job(target="isolated", model=None)
        executor(job)

        call_kwargs = mock_agent_loop.call_args[1]
        assert call_kwargs["llm_config"].model == "claude-sonnet-4-6"

    def test_isolated_without_agent_def_raises(self):
        """Isolated execution without agent_def should raise RuntimeError."""
        executor = JobExecutor()

        job = _make_job(target="isolated")
        with pytest.raises(RuntimeError, match="no agent definition"):
            executor(job)

    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_passes_use_containers(self, mock_agent_loop):
        """The use_containers flag should be passed through to run_agent_loop."""
        mock_agent_loop.return_value = FakeAgentResult()
        agent_def = FakeAgentDef()
        executor = JobExecutor(agent_def=agent_def, use_containers=True)

        job = _make_job(target="isolated")
        executor(job)

        call_kwargs = mock_agent_loop.call_args[1]
        assert call_kwargs["use_containers"] is True


# -- Isolated execution with delivery --


class TestExecuteIsolatedDelivery:
    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_none_delivery(self, mock_agent_loop):
        """Isolated job with 'none' delivery should not call channel_send."""
        mock_agent_loop.return_value = FakeAgentResult(text="result")
        agent_def = FakeAgentDef()
        channel_send = MagicMock()
        executor = JobExecutor(agent_def=agent_def, channel_send=channel_send)

        job = _make_job(target="isolated", delivery_mode="none")
        executor(job)

        channel_send.assert_not_called()

    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_announce_delivery(self, mock_agent_loop):
        """Isolated job with 'announce' delivery should send output to the channel."""
        mock_agent_loop.return_value = FakeAgentResult(text="Agent says hello")
        agent_def = FakeAgentDef()
        channel_send = MagicMock()
        executor = JobExecutor(agent_def=agent_def, channel_send=channel_send)

        job = _make_job(
            target="isolated",
            delivery_mode="announce",
            channel="whatsapp",
        )
        executor(job)

        channel_send.assert_called_once_with("whatsapp", "Agent says hello")

    @patch("httpx.post")
    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_webhook_delivery(self, mock_agent_loop, mock_post):
        """Isolated job with 'webhook' delivery should POST output to the URL."""
        mock_agent_loop.return_value = FakeAgentResult(text="result payload")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        agent_def = FakeAgentDef()
        executor = JobExecutor(agent_def=agent_def)

        job = _make_job(
            target="isolated",
            delivery_mode="webhook",
            url="https://hooks.example.com/notify",
        )
        executor(job)

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[1]["json"]["output"] == "result payload"

    @patch("creel.cron.executor.run_agent_loop")
    def test_isolated_delivery_best_effort_on_failure(self, mock_agent_loop):
        """If delivery fails with best_effort=True, execution should not raise."""
        mock_agent_loop.return_value = FakeAgentResult(text="result")
        agent_def = FakeAgentDef()
        channel_send = MagicMock(side_effect=RuntimeError("channel broken"))
        executor = JobExecutor(agent_def=agent_def, channel_send=channel_send)

        job = CronJob(
            name="test",
            schedule=Schedule(kind="cron", expr="0 8 * * *"),
            target="isolated",
            payload=Payload(message="do stuff"),
            delivery=Delivery(mode="announce", channel="whatsapp", best_effort=True),
        )

        # Should not raise despite delivery failure
        executor(job)


# -- Integration with CronManager --


class TestExecutorWithManager:
    @patch("creel.cron.executor.run_agent_loop")
    def test_executor_as_callable(self, mock_agent_loop):
        """JobExecutor should be usable as the CronManager executor callback."""
        mock_agent_loop.return_value = FakeAgentResult()
        agent_def = FakeAgentDef()
        executor = JobExecutor(agent_def=agent_def)

        # Simulate what CronManager._execute_job does
        job = _make_job(target="isolated")
        executor(job)

        mock_agent_loop.assert_called_once()

    def test_executor_dispatches_by_target(self):
        """JobExecutor should route to main or isolated based on job.target."""
        inject_event = MagicMock()
        executor = JobExecutor(inject_event=inject_event)

        main_job = _make_job(target="main")
        executor(main_job)
        inject_event.assert_called_once()

    @patch("creel.cron.executor.run_agent_loop")
    def test_model_copy_does_not_mutate_original(self, mock_agent_loop):
        """Model override should not mutate the original agent_def config."""
        mock_agent_loop.return_value = FakeAgentResult()
        agent_def = FakeAgentDef(model="claude-sonnet-4-6")
        executor = JobExecutor(agent_def=agent_def)

        job = _make_job(target="isolated", model="claude-haiku-4-5")
        executor(job)

        # Original should be unchanged
        assert agent_def.llm.model == "claude-sonnet-4-6"
