"""Tests for the warm container pool."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from creel.container_pool import (
    ContainerPool,
    ContainerPoolConfig,
    ManagedContainer,
)
from creel.skills.registry import SkillRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_container(
    alive: bool = True,
    ping_ok: bool = True,
    reset_ok: bool = True,
) -> ManagedContainer:
    """Create a ManagedContainer with a mock process."""
    proc = MagicMock()
    proc.poll.return_value = None if alive else 1
    proc.stdin = StringIO()
    proc.stdin.flush = lambda: None
    proc.stderr = StringIO("")
    proc.wait.return_value = 0

    container = ManagedContainer(
        id="test-123",
        image="llm-runner:latest",
        entrypoint="agent_runner.py",
        proc=proc,
        env_file_path="/tmp/test.env",
    )

    # Mock ping/reset since they depend on I/O
    container.ping = MagicMock(return_value=ping_ok)
    container.reset = MagicMock(return_value=reset_ok)
    container.shutdown = MagicMock()
    container.force_kill = MagicMock()

    return container


def _make_pool(
    enabled: bool = True,
    idle_timeout: int = 300,
    max_containers: int = 2,
) -> ContainerPool:
    """Create a ContainerPool with the idle reaper disabled for testing."""
    config = ContainerPoolConfig(
        enabled=enabled,
        idle_timeout_seconds=idle_timeout,
        max_containers=max_containers,
    )
    pool = ContainerPool(config)
    # Cancel the idle reaper to avoid flaky timer-based tests
    if pool._idle_timer:
        pool._idle_timer.cancel()
    return pool


# ---------------------------------------------------------------------------
# ManagedContainer tests
# ---------------------------------------------------------------------------


class TestManagedContainer:
    def test_alive_when_running(self):
        proc = MagicMock()
        proc.poll.return_value = None
        c = ManagedContainer(id="x", image="img", entrypoint="e", proc=proc, env_file_path="/tmp/x")
        assert c.alive is True

    def test_not_alive_when_exited(self):
        proc = MagicMock()
        proc.poll.return_value = 0
        c = ManagedContainer(id="x", image="img", entrypoint="e", proc=proc, env_file_path="/tmp/x")
        assert c.alive is False

    def test_send_writes_json_line(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = StringIO()
        proc.stdin.flush = lambda: None

        c = ManagedContainer(id="x", image="img", entrypoint="e", proc=proc, env_file_path="/tmp/x")
        c.send({"type": "ping"})

        proc.stdin.seek(0)
        line = proc.stdin.readline()
        parsed = json.loads(line)
        assert parsed["type"] == "ping"

    def test_recv_reads_json_line(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout = StringIO(json.dumps({"type": "pong"}) + "\n")

        c = ManagedContainer(id="x", image="img", entrypoint="e", proc=proc, env_file_path="/tmp/x")
        msg = c.recv()
        assert msg["type"] == "pong"

    def test_recv_raises_on_eof(self):
        proc = MagicMock()
        proc.poll.return_value = 137
        proc.stdout = StringIO("")
        proc.stderr = StringIO("out of memory")

        c = ManagedContainer(id="x", image="img", entrypoint="e", proc=proc, env_file_path="/tmp/x")
        with pytest.raises(RuntimeError, match="exited unexpectedly"):
            c.recv()

    def test_shutdown_sends_message(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = StringIO()
        proc.stdin.flush = lambda: None
        proc.wait.return_value = 0

        c = ManagedContainer(id="x", image="img", entrypoint="e", proc=proc, env_file_path="/tmp/x")
        c.shutdown()

        proc.stdin.seek(0)
        line = proc.stdin.readline()
        parsed = json.loads(line)
        assert parsed["type"] == "shutdown"
        proc.wait.assert_called_once()


# ---------------------------------------------------------------------------
# ContainerPool tests
# ---------------------------------------------------------------------------


class TestContainerPool:
    def test_disabled_pool_starts_no_reaper(self):
        pool = _make_pool(enabled=False)
        assert pool.enabled is False
        pool.shutdown()

    def test_stats_empty_pool(self):
        pool = _make_pool()
        stats = pool.stats()
        assert stats["total_containers"] == 0
        assert stats["idle_containers"] == 0
        assert stats["enabled"] is True
        pool.shutdown()

    @patch("creel.container_pool.subprocess.Popen")
    def test_acquire_starts_new_container(self, mock_popen):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = StringIO()
        proc.stdin.flush = lambda: None
        proc.stdout = StringIO("")
        proc.stderr = StringIO("")
        mock_popen.return_value = proc

        pool = _make_pool()

        container = pool.acquire(
            image="llm-runner:latest",
            entrypoint="agent_runner.py",
            docker_flags=["--read-only"],
            env_vars={"KEY": "val"},
        )

        assert container is not None
        assert container.image == "llm-runner:latest"
        assert container.entrypoint == "agent_runner.py"
        mock_popen.assert_called_once()

        # Check docker command includes our flags
        docker_cmd = mock_popen.call_args[0][0]
        assert "docker" in docker_cmd[0]
        assert "--read-only" in docker_cmd
        assert "-i" in docker_cmd
        assert "--rm" not in docker_cmd  # pool containers should NOT have --rm

        stats = pool.stats()
        assert stats["total_containers"] == 1
        pool.shutdown()

    def test_release_makes_container_idle(self):
        pool = _make_pool()
        container = _make_container()

        # Manually add to _all tracking
        pool._all.append(container)

        pool.release(container)

        key = (container.image, container.entrypoint)
        assert key in pool._idle
        assert container in pool._idle[key]
        container.reset.assert_called_once()
        pool.shutdown()

    def test_release_discards_failed_reset(self):
        pool = _make_pool()
        container = _make_container(reset_ok=False)
        pool._all.append(container)

        pool.release(container)

        key = (container.image, container.entrypoint)
        assert key not in pool._idle or container not in pool._idle.get(key, [])
        pool.shutdown()

    def test_release_discards_dead_container(self):
        pool = _make_pool()
        container = _make_container(alive=False)
        container.proc.poll.return_value = 1
        pool._all.append(container)

        pool.release(container)

        # Should not be in idle pool
        key = (container.image, container.entrypoint)
        idle = pool._idle.get(key, [])
        assert container not in idle
        pool.shutdown()

    def test_acquire_reuses_idle_container(self):
        pool = _make_pool()
        container = _make_container()
        pool._all.append(container)

        key = (container.image, container.entrypoint)
        pool._idle[key] = [container]

        reused = pool.acquire(
            image=container.image,
            entrypoint=container.entrypoint,
            docker_flags=[],
            env_vars={},
        )

        assert reused is container
        container.ping.assert_called_once()
        assert pool._idle[key] == []
        pool.shutdown()

    @patch("creel.container_pool.subprocess.run")
    @patch("creel.container_pool.subprocess.Popen")
    def test_acquire_skips_dead_idle_container(self, mock_popen, mock_run):
        pool = _make_pool()

        dead = _make_container(alive=True, ping_ok=False)
        pool._all.append(dead)
        key = (dead.image, dead.entrypoint)
        pool._idle[key] = [dead]

        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = StringIO()
        proc.stdin.flush = lambda: None
        proc.stdout = StringIO("")
        proc.stderr = StringIO("")
        mock_popen.return_value = proc

        new = pool.acquire(
            image=dead.image,
            entrypoint=dead.entrypoint,
            docker_flags=[],
            env_vars={},
        )

        assert new is not dead
        mock_popen.assert_called_once()

        pool.shutdown()

    def test_release_evicts_when_pool_full(self):
        pool = _make_pool(max_containers=1)

        c1 = _make_container()
        c1.id = "c1"
        c2 = _make_container()
        c2.id = "c2"
        pool._all.extend([c1, c2])

        key = (c1.image, c1.entrypoint)
        pool._idle[key] = [c1]

        pool.release(c2)

        # c1 should have been evicted, c2 should be in idle
        idle = pool._idle[key]
        assert c2 in idle
        assert c1 not in idle
        pool.shutdown()

    def test_shutdown_cleans_all_containers(self):
        pool = _make_pool()
        c1 = _make_container()
        c2 = _make_container()
        c2.id = "test-456"
        pool._all.extend([c1, c2])
        pool._idle[(c1.image, c1.entrypoint)] = [c1]

        pool.shutdown()

        c1.shutdown.assert_called()
        c2.shutdown.assert_called()
        assert len(pool._all) == 0
        assert len(pool._idle) == 0

    def test_release_after_shutdown_discards(self):
        pool = _make_pool()
        pool.shutdown()

        container = _make_container()
        pool.release(container)

        container.shutdown.assert_called()


# ---------------------------------------------------------------------------
# Protocol extension tests (ping/reset/shutdown in agent_runner)
# ---------------------------------------------------------------------------


class TestAgentRunnerKeepAlive:
    """Test the keepalive protocol extensions in agent_runner.py."""

    def test_ping_pong(self):
        """Agent runner main loop responds to ping with pong."""
        from io import StringIO

        from llm.agent_runner import main

        stdin_data = json.dumps({"type": "ping"}) + "\n" + json.dumps({"type": "shutdown"}) + "\n"
        stdout = StringIO()

        with (
            patch("llm.agent_runner.sys.stdin", StringIO(stdin_data)),
            patch("llm.agent_runner.sys.stdout", stdout),
            patch("llm.agent_runner.get_container_provider"),
        ):
            main()

        stdout.seek(0)
        msg = json.loads(stdout.readline())
        assert msg["type"] == "pong"

    def test_reset_ready(self):
        """Agent runner main loop responds to reset with ready."""
        from io import StringIO

        from llm.agent_runner import main

        stdin_data = json.dumps({"type": "reset"}) + "\n" + json.dumps({"type": "shutdown"}) + "\n"
        stdout = StringIO()

        with (
            patch("llm.agent_runner.sys.stdin", StringIO(stdin_data)),
            patch("llm.agent_runner.sys.stdout", stdout),
            patch("llm.agent_runner.get_container_provider"),
        ):
            main()

        stdout.seek(0)
        msg = json.loads(stdout.readline())
        assert msg["type"] == "ready"

    def test_shutdown_exits_cleanly(self):
        """Shutdown message causes main loop to exit."""
        from io import StringIO

        from llm.agent_runner import main

        stdin_data = json.dumps({"type": "shutdown"}) + "\n"
        stdout = StringIO()

        with (
            patch("llm.agent_runner.sys.stdin", StringIO(stdin_data)),
            patch("llm.agent_runner.sys.stdout", stdout),
            patch("llm.agent_runner.get_container_provider"),
        ):
            main()

        # No output expected — shutdown just exits
        stdout.seek(0)
        assert stdout.read() == ""


# ---------------------------------------------------------------------------
# Runner keepalive protocol tests
# ---------------------------------------------------------------------------


class TestRunnerKeepAlive:
    """Test the keepalive protocol in runner.py."""

    def test_ping_pong(self):
        """Runner keepalive loop responds to ping with pong."""
        from io import StringIO

        from llm.runner import main

        stdin_data = json.dumps({"type": "ping"}) + "\n" + json.dumps({"type": "shutdown"}) + "\n"
        stdout = StringIO()

        with (
            patch("llm.runner.get_container_provider"),
            patch("llm.runner.sys.stdin", StringIO(stdin_data)),
            patch("llm.runner.sys.stdout", stdout),
        ):
            main()

        stdout.seek(0)
        msg = json.loads(stdout.readline())
        assert msg["type"] == "pong"

    def test_reset_ready(self):
        """Runner keepalive loop responds to reset with ready."""
        from io import StringIO

        from llm.runner import main

        stdin_data = json.dumps({"type": "reset"}) + "\n" + json.dumps({"type": "shutdown"}) + "\n"
        stdout = StringIO()

        with (
            patch("llm.runner.get_container_provider"),
            patch("llm.runner.sys.stdin", StringIO(stdin_data)),
            patch("llm.runner.sys.stdout", stdout),
        ):
            main()

        stdout.seek(0)
        msg = json.loads(stdout.readline())
        assert msg["type"] == "ready"


# ---------------------------------------------------------------------------
# Integration with container_agent tests
# ---------------------------------------------------------------------------


class TestContainerAgentPoolIntegration:
    """Test that container_agent correctly uses the pool."""

    def test_run_with_pool_acquires_and_releases(self):
        """Pool acquire/release should be called around protocol execution."""
        from creel.container_agent import _run_with_pool

        pool = MagicMock()
        container = _make_container()

        # Set up container to return a final message
        final_msg = {
            "type": "final",
            "text": "Hello!",
            "turns_used": 1,
            "tool_calls_made": 0,
            "stop_reason": "end_turn",
            "tool_history": [],
            "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Hello!"}]}],
        }
        container.send = MagicMock()
        container.recv = MagicMock(return_value=final_msg)

        pool.acquire.return_value = container

        result = _run_with_pool(
            pool=pool,
            start_msg={"type": "start", "messages": []},
            messages=[],
            registry=SkillRegistry(),
            skill_overrides={},
            use_containers=False,
            guardian=None,
            confirm_action=None,
            memory_manager=None,
            bridge_config=None,
            session_state=None,
            env_vars={"ANTHROPIC_API_KEY": "test"},
        )

        assert result.text == "Hello!"
        pool.acquire.assert_called_once()
        pool.release.assert_called_once_with(container)

    def test_run_with_pool_kills_on_error(self):
        """On protocol error, container should be killed, not released."""
        from creel.container_agent import _run_with_pool

        pool = MagicMock()
        container = _make_container()
        container.send = MagicMock()
        container.recv = MagicMock(side_effect=RuntimeError("boom"))

        pool.acquire.return_value = container

        result = _run_with_pool(
            pool=pool,
            start_msg={"type": "start", "messages": []},
            messages=[],
            registry=SkillRegistry(),
            skill_overrides={},
            use_containers=False,
            guardian=None,
            confirm_action=None,
            memory_manager=None,
            bridge_config=None,
            session_state=None,
            env_vars={},
        )

        assert result.stop_reason == "error"
        pool.release.assert_not_called()
        container.force_kill.assert_called_once()
