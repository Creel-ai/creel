"""Tests for the sub-agent system."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.models import AgentConfig, LLMConfig, ToolConfig, ToolParameter
from taskrunner.subagents.executor import handle_subagent_tool
from taskrunner.subagents.manager import SubAgentManager
from taskrunner.subagents.models import SubAgentConfig, SubAgentInfo, SubAgentStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_llm_config() -> LLMConfig:
    return LLMConfig(model="claude-sonnet-4-20250514", max_tokens=1024)


def _make_tools() -> dict[str, ToolConfig]:
    return {
        "check_weather": ToolConfig(
            executor="weather",
            description="Get weather",
            parameters={
                "location": ToolParameter(type="string", description="City", required=True),
            },
        ),
    }


def _make_agent_config() -> AgentConfig:
    return AgentConfig(max_turns=5)


def _mock_agent_result(text: str = "Done."):
    """Return a mock AgentResult."""
    from taskrunner.agent import AgentResult

    return AgentResult(
        text=text,
        turns_used=1,
        tool_calls_made=0,
        stop_reason="end_turn",
    )


def _make_manager(
    result_callback=None,
    llm_config=None,
    tools_config=None,
    agent_config=None,
) -> SubAgentManager:
    return SubAgentManager(
        llm_config=llm_config or _make_llm_config(),
        tools_config=tools_config or _make_tools(),
        agent_config=agent_config or _make_agent_config(),
        system_prompt="You are a test agent.",
        result_callback=result_callback,
    )


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestSubAgentModels:
    def test_config_defaults(self):
        cfg = SubAgentConfig(task="do something")
        assert cfg.task == "do something"
        assert cfg.label == ""
        assert cfg.model is None
        assert cfg.timeout_seconds == 300

    def test_config_custom(self):
        cfg = SubAgentConfig(task="build it", label="builder", model="claude-haiku-4-5-20251001", timeout_seconds=60)
        assert cfg.label == "builder"
        assert cfg.model == "claude-haiku-4-5-20251001"
        assert cfg.timeout_seconds == 60

    def test_config_timeout_bounds(self):
        with pytest.raises(Exception):
            SubAgentConfig(task="x", timeout_seconds=5)  # below ge=10
        with pytest.raises(Exception):
            SubAgentConfig(task="x", timeout_seconds=7200)  # above le=3600

    def test_status_enum(self):
        assert SubAgentStatus.RUNNING.value == "running"
        assert SubAgentStatus.COMPLETED.value == "completed"
        assert SubAgentStatus.FAILED.value == "failed"
        assert SubAgentStatus.KILLED.value == "killed"
        assert SubAgentStatus.TIMEOUT.value == "timeout"

    def test_info_defaults(self):
        info = SubAgentInfo(id="abc", label="test", status=SubAgentStatus.RUNNING)
        assert info.id == "abc"
        assert info.completed_at is None
        assert info.result_summary == ""
        assert info.error == ""


# ---------------------------------------------------------------------------
# Manager tests
# ---------------------------------------------------------------------------


class TestSubAgentManager:
    @patch("taskrunner.agent.run_agent_loop", side_effect=ImportError)
    def test_spawn_returns_agent_id(self, _mock):
        """spawn() should return an ID immediately without blocking."""
        manager = _make_manager()
        config = SubAgentConfig(task="test task")
        agent_id = manager.spawn(config)

        assert isinstance(agent_id, str)
        assert len(agent_id) == 8  # token_hex(4) -> 8 chars

    @patch("taskrunner.agent.run_agent_loop")
    def test_spawn_creates_running_agent(self, mock_loop):
        mock_loop.return_value = _mock_agent_result("All done")
        manager = _make_manager()
        agent_id = manager.spawn(SubAgentConfig(task="hello"))

        # Give thread time to start
        time.sleep(0.1)
        info = manager.get(agent_id)
        assert info is not None
        assert info.id == agent_id

    @patch("taskrunner.agent.run_agent_loop")
    def test_list_agents_returns_all(self, mock_loop):
        mock_loop.return_value = _mock_agent_result()
        manager = _make_manager()

        id1 = manager.spawn(SubAgentConfig(task="task 1", label="first"))
        id2 = manager.spawn(SubAgentConfig(task="task 2", label="second"))

        time.sleep(0.2)
        agents = manager.list_agents()
        ids = {a.id for a in agents}
        assert id1 in ids
        assert id2 in ids

    @patch("taskrunner.agent.run_agent_loop")
    def test_list_agents_newest_first(self, mock_loop):
        mock_loop.return_value = _mock_agent_result()
        manager = _make_manager()

        id1 = manager.spawn(SubAgentConfig(task="task 1"))
        time.sleep(0.05)
        id2 = manager.spawn(SubAgentConfig(task="task 2"))

        time.sleep(0.2)
        agents = manager.list_agents()
        assert len(agents) >= 2
        # newest first
        assert agents[0].id == id2

    @patch("taskrunner.agent.run_agent_loop")
    def test_agent_completes_with_result(self, mock_loop):
        mock_loop.return_value = _mock_agent_result("The answer is 42.")
        callback = MagicMock()
        manager = _make_manager(result_callback=callback)

        agent_id = manager.spawn(SubAgentConfig(task="compute"))
        time.sleep(0.3)

        info = manager.get(agent_id)
        assert info is not None
        assert info.status == SubAgentStatus.COMPLETED
        assert "42" in info.result_summary
        assert info.completed_at is not None

    @patch("taskrunner.agent.run_agent_loop")
    def test_result_callback_fires_on_completion(self, mock_loop):
        mock_loop.return_value = _mock_agent_result("result text")
        callback = MagicMock()
        manager = _make_manager(result_callback=callback)

        agent_id = manager.spawn(SubAgentConfig(task="test"))
        time.sleep(0.3)

        callback.assert_called_once_with(agent_id, "result text")

    @patch("taskrunner.agent.run_agent_loop")
    def test_agent_failure_sets_status(self, mock_loop):
        mock_loop.side_effect = RuntimeError("LLM exploded")
        callback = MagicMock()
        manager = _make_manager(result_callback=callback)

        agent_id = manager.spawn(SubAgentConfig(task="fail"))
        time.sleep(0.3)

        info = manager.get(agent_id)
        assert info is not None
        assert info.status == SubAgentStatus.FAILED
        assert "LLM exploded" in info.error
        callback.assert_called_once()

    @patch("taskrunner.agent.run_agent_loop")
    def test_kill_terminates_agent(self, mock_loop):
        # Make the agent loop block until cancelled
        cancel_event = threading.Event()

        def blocking_loop(**kwargs):
            cancel_event.wait(timeout=10)
            return _mock_agent_result("interrupted")

        mock_loop.side_effect = blocking_loop
        manager = _make_manager()

        agent_id = manager.spawn(SubAgentConfig(task="long task"))
        time.sleep(0.1)

        result = manager.kill(agent_id)
        assert result is True
        # Signal the blocking mock to unblock
        cancel_event.set()

        time.sleep(0.2)
        info = manager.get(agent_id)
        assert info is not None
        assert info.status == SubAgentStatus.KILLED

    @patch("taskrunner.agent.run_agent_loop")
    def test_kill_nonexistent_returns_false(self, mock_loop):
        manager = _make_manager()
        assert manager.kill("nonexistent") is False

    @patch("taskrunner.agent.run_agent_loop")
    def test_kill_completed_returns_false(self, mock_loop):
        mock_loop.return_value = _mock_agent_result()
        manager = _make_manager()

        agent_id = manager.spawn(SubAgentConfig(task="quick"))
        time.sleep(0.3)

        assert manager.kill(agent_id) is False

    @patch("taskrunner.agent.run_agent_loop")
    def test_steer_queues_message(self, mock_loop):
        call_count = 0

        def counting_loop(**kwargs):
            nonlocal call_count
            call_count += 1
            return _mock_agent_result(f"result {call_count}")

        mock_loop.side_effect = counting_loop
        manager = _make_manager()

        agent_id = manager.spawn(SubAgentConfig(task="start"))
        # Queue a steer message immediately (it may or may not get picked up
        # depending on timing, but the call should succeed)
        time.sleep(0.05)
        info = manager.get(agent_id)
        if info and info.status == SubAgentStatus.RUNNING:
            ok = manager.steer(agent_id, "also do this")
            assert ok is True

        time.sleep(0.3)

    @patch("taskrunner.agent.run_agent_loop")
    def test_steer_nonrunning_returns_false(self, mock_loop):
        mock_loop.return_value = _mock_agent_result()
        manager = _make_manager()

        agent_id = manager.spawn(SubAgentConfig(task="quick"))
        time.sleep(0.3)

        assert manager.steer(agent_id, "too late") is False

    @patch("taskrunner.agent.run_agent_loop")
    def test_timeout_sets_status(self, mock_loop):
        """Agent should be marked TIMEOUT when it exceeds timeout_seconds."""
        block = threading.Event()

        def slow_loop(**kwargs):
            block.wait(timeout=10)
            return _mock_agent_result("late")

        mock_loop.side_effect = slow_loop
        manager = _make_manager()

        agent_id = manager.spawn(SubAgentConfig(task="slow", timeout_seconds=10))
        # Manually trigger timeout handler (faster than waiting 10s)
        manager._handle_timeout(agent_id)

        info = manager.get(agent_id)
        assert info is not None
        assert info.status == SubAgentStatus.TIMEOUT
        assert info.error == "Timed out"
        block.set()  # unblock the thread

    @patch("taskrunner.agent.run_agent_loop")
    def test_concurrent_spawns(self, mock_loop):
        """Multiple sub-agents can run concurrently."""
        results = {}

        def per_task_loop(**kwargs):
            messages = kwargs.get("messages", [])
            task = messages[0]["content"] if messages else "unknown"
            time.sleep(0.05)
            return _mock_agent_result(f"done: {task}")

        mock_loop.side_effect = per_task_loop
        callback = MagicMock()
        manager = _make_manager(result_callback=callback)

        ids = []
        for i in range(3):
            agent_id = manager.spawn(SubAgentConfig(task=f"task-{i}", label=f"worker-{i}"))
            ids.append(agent_id)

        time.sleep(0.5)

        for agent_id in ids:
            info = manager.get(agent_id)
            assert info is not None
            assert info.status == SubAgentStatus.COMPLETED

        assert callback.call_count == 3

    @patch("taskrunner.agent.run_agent_loop")
    def test_model_override(self, mock_loop):
        """Spawn with model override should pass different LLMConfig."""
        captured_config = {}

        def capture_loop(**kwargs):
            captured_config.update(kwargs)
            return _mock_agent_result("ok")

        mock_loop.side_effect = capture_loop
        manager = _make_manager()

        manager.spawn(SubAgentConfig(task="x", model="claude-haiku-4-5-20251001"))
        time.sleep(0.2)

        assert captured_config.get("llm_config") is not None
        assert captured_config["llm_config"].model == "claude-haiku-4-5-20251001"

    @patch("taskrunner.agent.run_agent_loop")
    def test_result_truncated_at_2000_chars(self, mock_loop):
        long_text = "x" * 3000
        mock_loop.return_value = _mock_agent_result(long_text)
        manager = _make_manager()

        agent_id = manager.spawn(SubAgentConfig(task="big"))
        time.sleep(0.3)

        info = manager.get(agent_id)
        assert info is not None
        assert len(info.result_summary) <= 2003 + 3  # 2000 + "..."

    @patch("taskrunner.agent.run_agent_loop")
    def test_get_nonexistent_returns_none(self, mock_loop):
        manager = _make_manager()
        assert manager.get("nope") is None

    @patch("taskrunner.agent.run_agent_loop")
    def test_default_label(self, mock_loop):
        mock_loop.return_value = _mock_agent_result()
        manager = _make_manager()

        agent_id = manager.spawn(SubAgentConfig(task="test"))
        time.sleep(0.2)

        info = manager.get(agent_id)
        assert info is not None
        assert info.label == f"subagent-{agent_id}"


# ---------------------------------------------------------------------------
# Executor / tool dispatch tests
# ---------------------------------------------------------------------------


class TestSubAgentExecutor:
    def _manager(self):
        return _make_manager()

    @patch("taskrunner.agent.run_agent_loop")
    def test_spawn_action(self, mock_loop):
        mock_loop.return_value = _mock_agent_result("spawned ok")
        manager = self._manager()

        result = json.loads(handle_subagent_tool(
            {"action": "spawn", "task": "build something", "label": "builder"},
            manager,
        ))

        assert "agent_id" in result
        assert result["status"] == "running"
        assert result["label"] == "builder"
        time.sleep(0.2)

    def test_spawn_missing_task(self):
        manager = self._manager()
        result = json.loads(handle_subagent_tool({"action": "spawn"}, manager))
        assert "error" in result

    @patch("taskrunner.agent.run_agent_loop")
    def test_list_action_empty(self, mock_loop):
        manager = self._manager()
        result = json.loads(handle_subagent_tool({"action": "list"}, manager))
        assert result["agents"] == []

    @patch("taskrunner.agent.run_agent_loop")
    def test_list_action_with_agents(self, mock_loop):
        mock_loop.return_value = _mock_agent_result("done")
        manager = self._manager()

        handle_subagent_tool(
            {"action": "spawn", "task": "test", "label": "worker"},
            manager,
        )
        time.sleep(0.3)

        result = json.loads(handle_subagent_tool({"action": "list"}, manager))
        assert len(result["agents"]) == 1
        assert result["agents"][0]["label"] == "worker"

    @patch("taskrunner.agent.run_agent_loop")
    def test_kill_action(self, mock_loop):
        block = threading.Event()

        def blocking(**kwargs):
            block.wait(timeout=10)
            return _mock_agent_result()

        mock_loop.side_effect = blocking
        manager = self._manager()

        spawn_result = json.loads(handle_subagent_tool(
            {"action": "spawn", "task": "long"},
            manager,
        ))
        agent_id = spawn_result["agent_id"]
        time.sleep(0.1)

        kill_result = json.loads(handle_subagent_tool(
            {"action": "kill", "agent_id": agent_id},
            manager,
        ))
        assert kill_result["status"] == "killed"
        block.set()
        time.sleep(0.1)

    def test_kill_missing_id(self):
        manager = self._manager()
        result = json.loads(handle_subagent_tool({"action": "kill"}, manager))
        assert "error" in result

    def test_steer_missing_id(self):
        manager = self._manager()
        result = json.loads(handle_subagent_tool(
            {"action": "steer", "message": "hi"},
            manager,
        ))
        assert "error" in result

    def test_steer_missing_message(self):
        manager = self._manager()
        result = json.loads(handle_subagent_tool(
            {"action": "steer", "agent_id": "abc"},
            manager,
        ))
        assert "error" in result

    def test_unknown_action(self):
        manager = self._manager()
        result = json.loads(handle_subagent_tool({"action": "dance"}, manager))
        assert "error" in result
        assert "dance" in result["error"]


# ---------------------------------------------------------------------------
# Integration: tools.py dispatch
# ---------------------------------------------------------------------------


class TestToolsIntegration:
    @patch("taskrunner.agent.run_agent_loop")
    def test_execute_tool_call_dispatches_subagent(self, mock_loop):
        """execute_tool_call should dispatch to subagent handler."""
        mock_loop.return_value = _mock_agent_result("sub done")
        from taskrunner.tools import execute_tool_call

        manager = _make_manager()
        result = execute_tool_call(
            tool_name="subagent",
            tool_input={"action": "list"},
            tools_config=_make_tools(),
            subagent_manager=manager,
        )

        parsed = json.loads(result)
        assert "agents" in parsed

    def test_execute_tool_call_without_manager_raises(self):
        """Without a manager, 'subagent' should fall through to unknown tool."""
        from taskrunner.tools import execute_tool_call

        with pytest.raises(ValueError, match="Unknown tool"):
            execute_tool_call(
                tool_name="subagent",
                tool_input={"action": "list"},
                tools_config=_make_tools(),
            )


# ---------------------------------------------------------------------------
# Tool definition tests
# ---------------------------------------------------------------------------


class TestToolDefinitions:
    def test_subagent_tool_included_when_enabled(self):
        from taskrunner.tools import build_tool_definitions

        defs = build_tool_definitions(_make_tools(), include_subagent_tool=True)
        names = [d["name"] for d in defs]
        assert "subagent" in names

    def test_subagent_tool_excluded_by_default(self):
        from taskrunner.tools import build_tool_definitions

        defs = build_tool_definitions(_make_tools())
        names = [d["name"] for d in defs]
        assert "subagent" not in names

    def test_subagent_tool_schema_has_action(self):
        from taskrunner.tools import BUILTIN_SUBAGENT_TOOL

        schema = BUILTIN_SUBAGENT_TOOL["input_schema"]
        assert "action" in schema["properties"]
        assert schema["required"] == ["action"]
        assert "spawn" in schema["properties"]["action"]["enum"]
