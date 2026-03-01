"""Host-side orchestrator for the containerized agent loop.

Manages the LLM container via subprocess, mediating tool execution and
Guardian checks over JSON-over-stdio. The container only has the Anthropic
API key; all secrets, Guardian, and executor management stay on the host.

Supports warm container pooling: when a ContainerPool is provided,
containers are reused across requests to eliminate cold-start latency.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, Any

from guardian.types import ActionVerdict
from taskrunner.agent import (
    AgentResult,
    PendingApproval,
    _ensure_tool_call_integrity,
    _extract_prior_tools,
    _extract_user_request_for_coherence,
)
from taskrunner.models import AgentConfig, BridgeConfig, LLMConfig, ToolConfig
from taskrunner.orchestrator import _ensure_image
from taskrunner.tools import build_tool_definitions, execute_tool_call

if TYPE_CHECKING:
    from collections.abc import Callable

    from taskrunner.container_pool import ContainerPool, ManagedContainer

logger = logging.getLogger(__name__)

# Container timeout: generous to allow multi-turn agent loops
_CONTAINER_TIMEOUT = 600  # 10 minutes
_IMAGE = "llm-runner:latest"

# Docker security flags shared between cold-start and pooled containers
_AGENT_DOCKER_FLAGS = [
    "--read-only",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,size=16M",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--memory=512m",
    "--cpus=1.0",
]


def _get_llm_env_vars() -> dict[str, str]:
    """Collect LLM credential env vars from the current environment."""
    env_vars: dict[str, str] = {}
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if auth_token:
        env_vars["ANTHROPIC_AUTH_TOKEN"] = auth_token
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        env_vars["ANTHROPIC_API_KEY"] = api_key
    return env_vars


def run_agent_loop_container(
    messages: list[dict],
    llm_config: LLMConfig,
    tools_config: dict[str, ToolConfig],
    agent_config: AgentConfig,
    system_prompt: str | None = None,
    use_containers: bool = False,
    guardian: Any | None = None,
    confirm_action: Callable[[str, dict, str], bool] | None = None,
    memory_manager: Any | None = None,
    bridge_config: BridgeConfig | None = None,
    session_state: dict | None = None,
    container_pool: ContainerPool | None = None,
) -> AgentResult:
    """Run the agent loop inside an isolated Docker container.

    Same signature as run_agent_loop() for drop-in replacement.
    The container communicates via JSON-over-stdio. Tool execution,
    Guardian validation, and secret management all happen on the host.

    When *container_pool* is provided and enabled, a warm container is
    acquired from the pool (or started fresh), used for this session,
    then returned to the pool for reuse.
    """
    _ensure_tool_call_integrity(messages)
    _ensure_image(_IMAGE)

    include_memory = memory_manager is not None
    include_workspace = bool(
        tools_config and any(tc.executor == "file_ops" for tc in tools_config.values())
    )
    tool_defs = (
        build_tool_definitions(
            tools_config,
            include_memory_tools=include_memory,
            include_workspace_tools=include_workspace,
        )
        if tools_config
        else []
    )

    # Build the start message
    start_msg = {
        "type": "start",
        "messages": messages,
        "tools": tool_defs,
        "system": system_prompt,
        "model": llm_config.model,
        "max_tokens": llm_config.max_tokens,
        "max_turns": agent_config.max_turns,
    }

    env_vars = _get_llm_env_vars()

    # Use warm container pool if available and enabled
    if container_pool is not None and container_pool.enabled:
        return _run_with_pool(
            container_pool,
            start_msg,
            messages,
            tools_config,
            use_containers,
            guardian,
            confirm_action,
            memory_manager,
            bridge_config,
            session_state,
            env_vars,
        )

    # Cold-start path (original behavior)
    return _run_cold_start(
        start_msg,
        messages,
        tools_config,
        use_containers,
        guardian,
        confirm_action,
        memory_manager,
        bridge_config,
        session_state,
        env_vars,
    )


def _run_with_pool(
    pool: ContainerPool,
    start_msg: dict,
    messages: list[dict],
    tools_config: dict[str, ToolConfig],
    use_containers: bool,
    guardian: object | None,
    confirm_action: Callable[[str, dict, str], bool] | None,
    memory_manager: object | None,
    bridge_config: object | None,
    session_state: dict | None,
    env_vars: dict[str, str],
) -> AgentResult:
    """Run the agent loop using a warm container from the pool."""
    container = pool.acquire(
        image=_IMAGE,
        entrypoint="agent_runner.py",
        docker_flags=_AGENT_DOCKER_FLAGS,
        env_vars=env_vars,
    )

    try:
        result = _run_protocol_pooled(
            container,
            start_msg,
            messages,
            tools_config,
            use_containers,
            guardian,
            confirm_action,
            memory_manager,
            bridge_config,
            session_state,
        )
        # Return container to pool for reuse
        pool.release(container)
        return result
    except Exception as e:
        logger.exception("Pooled container agent protocol error")
        # Don't return broken containers to the pool — kill and remove
        # from pool tracking so it doesn't leak.
        container.force_kill()
        with pool._lock:
            pool._remove_container(container)
        return AgentResult(
            text=f"Container agent error: {e}",
            turns_used=0,
            tool_calls_made=0,
            stop_reason="error",
        )


def _run_cold_start(
    start_msg: dict,
    messages: list[dict],
    tools_config: dict[str, ToolConfig],
    use_containers: bool,
    guardian: object | None,
    confirm_action: Callable[[str, dict, str], bool] | None,
    memory_manager: object | None,
    bridge_config: object | None,
    session_state: dict | None,
    env_vars: dict[str, str],
) -> AgentResult:
    """Run the agent loop with a fresh ephemeral container (original path)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=True, prefix="creel-agent-"
    ) as env_file:
        for key, value in env_vars.items():
            env_file.write(f"{key}={value}\n")
        env_file.flush()

        proc = subprocess.Popen(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                *_AGENT_DOCKER_FLAGS,
                "--env-file",
                env_file.name,
                _IMAGE,
                "agent_runner.py",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            return _run_protocol(
                proc,
                start_msg,
                messages,
                tools_config,
                use_containers,
                guardian,
                confirm_action,
                memory_manager,
                bridge_config,
                session_state,
            )
        except Exception as e:
            logger.exception("Container agent protocol error")
            # Try to clean up
            proc.kill()
            proc.wait(timeout=5)
            return AgentResult(
                text=f"Container agent error: {e}",
                turns_used=0,
                tool_calls_made=0,
                stop_reason="error",
            )


def _send_to_container(proc: subprocess.Popen[str], msg: dict) -> None:
    """Write a JSON line to the container's stdin."""
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _recv_from_container(proc: subprocess.Popen[str]) -> dict:
    """Read a JSON line from the container's stdout."""
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        # Check if process died
        retcode = proc.poll()
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(
            f"Container exited unexpectedly (code={retcode}). stderr: {stderr[:500]}"
        )
    return json.loads(line)


def _run_protocol(
    proc: subprocess.Popen[str],
    start_msg: dict,
    messages: list[dict],
    tools_config: dict[str, ToolConfig],
    use_containers: bool,
    guardian: Any | None,
    confirm_action: Callable[[str, dict, str], bool] | None,
    memory_manager: Any | None,
    bridge_config: BridgeConfig | None = None,
    session_state: dict | None = None,
) -> AgentResult:
    """Run the JSON-over-stdio protocol with the container."""
    _send_to_container(proc, start_msg)

    while True:
        msg = _recv_from_container(proc)
        msg_type = msg.get("type")

        if msg_type == "final":
            # Replace session messages with full history from container
            # (includes all intermediate tool_use/tool_result turns)
            if msg.get("messages"):
                messages.clear()
                messages.extend(msg["messages"])
            elif msg.get("text"):
                # Fallback: at least save the final response
                messages.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": msg["text"]}],
                    }
                )
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=10)
            return AgentResult(
                text=msg["text"],
                turns_used=msg["turns_used"],
                tool_calls_made=msg["tool_calls_made"],
                stop_reason=msg["stop_reason"],
                tool_history=msg.get("tool_history", []),
                last_input_tokens=msg.get("last_input_tokens", 0),
            )

        elif msg_type == "error":
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=10)
            return AgentResult(
                text=msg["message"],
                turns_used=0,
                tool_calls_made=0,
                stop_reason="error",
            )

        elif msg_type == "tool_request":
            results, pending_result = _handle_tool_request(
                msg["calls"],
                tools_config,
                use_containers,
                guardian,
                confirm_action,
                memory_manager,
                messages,
                bridge_config=bridge_config,
                session_state=session_state,
            )

            # Check if any result requires async approval
            if results is None:
                # approval_required — we need to bail out
                proc.kill()
                proc.wait(timeout=5)
                assert pending_result is not None
                return pending_result

            _send_to_container(proc, {"type": "tool_results", "results": results})

        else:
            logger.warning("Unknown message type from container: %s", msg_type)
            proc.kill()
            proc.wait(timeout=5)
            return AgentResult(
                text=f"Unknown container message type: {msg_type}",
                turns_used=0,
                tool_calls_made=0,
                stop_reason="error",
            )


def _run_protocol_pooled(
    container: ManagedContainer,
    start_msg: dict,
    messages: list[dict],
    tools_config: dict[str, ToolConfig],
    use_containers: bool,
    guardian: object | None,
    confirm_action: Callable[[str, dict, str], bool] | None,
    memory_manager: object | None,
    bridge_config: object | None = None,
    session_state: dict | None = None,
) -> AgentResult:
    """Run the JSON-over-stdio protocol using a pooled ManagedContainer.

    Same logic as _run_protocol but uses ManagedContainer.send/recv
    and does NOT close stdin or wait for process exit (the container
    stays alive for reuse).
    """
    container.send(start_msg)

    while True:
        msg = container.recv()
        msg_type = msg.get("type")

        if msg_type == "final":
            if msg.get("messages"):
                messages.clear()
                messages.extend(msg["messages"])
            elif msg.get("text"):
                messages.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": msg["text"]}],
                    }
                )
            # Do NOT close stdin or wait — container stays alive
            return AgentResult(
                text=msg["text"],
                turns_used=msg["turns_used"],
                tool_calls_made=msg["tool_calls_made"],
                stop_reason=msg["stop_reason"],
                tool_history=msg.get("tool_history", []),
                last_input_tokens=msg.get("last_input_tokens", 0),
            )

        elif msg_type == "error":
            return AgentResult(
                text=msg["message"],
                turns_used=0,
                tool_calls_made=0,
                stop_reason="error",
            )

        elif msg_type == "tool_request":
            results, pending_result = _handle_tool_request(
                msg["calls"],
                tools_config,
                use_containers,
                guardian,
                confirm_action,
                memory_manager,
                messages,
                bridge_config=bridge_config,
                session_state=session_state,
            )

            if results is None:
                # approval_required — container will be discarded by caller
                return pending_result

            container.send({"type": "tool_results", "results": results})

        else:
            logger.warning("Unknown message type from container: %s", msg_type)
            return AgentResult(
                text=f"Unknown container message type: {msg_type}",
                turns_used=0,
                tool_calls_made=0,
                stop_reason="error",
            )


def _handle_tool_request(
    calls: list[dict],
    tools_config: dict[str, ToolConfig],
    use_containers: bool,
    guardian: Any | None,
    confirm_action: Callable[[str, dict, str], bool] | None,
    memory_manager: Any | None,
    messages: list[dict],
    bridge_config: BridgeConfig | None = None,
    session_state: dict | None = None,
) -> tuple[list[dict] | None, AgentResult | None]:
    """Process tool calls from the container, applying Guardian checks.

    Returns a tuple of (results, pending_result):
    - (results, None) on success
    - (None, AgentResult) when approval is required
    """
    results = []
    for call in calls:
        tool_name = call["name"]
        tool_input = call["input"]
        tool_id = call["id"]

        # Guardian action validation
        if guardian is not None:
            decision = guardian.validate_action(tool_name, tool_input)
            if decision.verdict == ActionVerdict.DENY:
                logger.warning("Guardian denied tool %s: %s", tool_name, decision.reason)
                guardian.log_action_outcome(tool_name, "deny", "denied_by_policy")
                results.append(
                    {
                        "tool_use_id": tool_id,
                        "content": f"Action denied by security policy: {decision.reason}",
                        "is_error": True,
                    }
                )
                continue

            if decision.verdict == ActionVerdict.REVIEW:
                logger.warning("Guardian review for tool %s: %s", tool_name, decision.reason)
                if confirm_action is not None:
                    if not confirm_action(tool_name, tool_input, decision.reason):
                        guardian.log_action_outcome(tool_name, "review", "denied_by_user")
                        results.append(
                            {
                                "tool_use_id": tool_id,
                                "content": f"Action denied by user: {decision.reason}",
                                "is_error": True,
                            }
                        )
                        continue
                    guardian.log_action_outcome(tool_name, "review", "approved_by_user")
                else:
                    # No callback — inject synthetic tool_results for ALL
                    # tool calls so session history stays valid.
                    assistant_tool_use = []
                    synthetic_results = []
                    for c in calls:
                        assistant_tool_use.append(
                            {
                                "type": "tool_use",
                                "id": c["id"],
                                "name": c["name"],
                                "input": c["input"],
                            }
                        )
                        if c["id"] == tool_id:
                            approval_msg = f"Action requires approval: {decision.reason}"
                        else:
                            approval_msg = (
                                "Action skipped — another tool in this batch requires approval."
                            )
                        synthetic_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": c["id"],
                                "content": approval_msg,
                                "is_error": True,
                            }
                        )

                    # Persist the blocked tool call + synthetic results into
                    # host-side session history before returning.
                    messages.append({"role": "assistant", "content": assistant_tool_use})
                    messages.append(
                        {
                            "role": "user",
                            "content": synthetic_results,
                        }
                    )
                    pending_result = AgentResult(
                        text="This action requires approval before proceeding.",
                        turns_used=0,
                        tool_calls_made=0,
                        stop_reason="approval_required",
                        pending_approval=PendingApproval(
                            tool_name=tool_name,
                            tool_input=tool_input,
                            reason=decision.reason,
                        ),
                    )
                    return None, pending_result

        # Guardian coherence check
        if guardian is not None and hasattr(guardian, "check_coherence"):
            user_request = _extract_user_request_for_coherence(messages)

            if user_request:
                prior_tools = _extract_prior_tools(messages)
                if prior_tools:
                    logger.info("Coherence context: prior_tools=%s for %s", prior_tools, tool_name)
                coherence = guardian.check_coherence(
                    user_request, tool_name, tool_input, prior_tools=prior_tools
                )
                if not coherence.coherent:
                    logger.warning(
                        "Guardian coherence check failed for %s: %s",
                        tool_name,
                        coherence.reasoning,
                    )
                    results.append(
                        {
                            "tool_use_id": tool_id,
                            "content": f"Action blocked — not coherent with user request: {coherence.reasoning}",
                            "is_error": True,
                        }
                    )
                    continue

        # Screen memory write content through Guardian before execution
        _MEMORY_WRITE_TOOLS = {"remember", "update_long_term_memory", "edit_memory"}
        if guardian is not None and tool_name in _MEMORY_WRITE_TOOLS:
            write_text = tool_input.get("text") or tool_input.get("new_text") or ""
            if write_text:
                screen_result = guardian.screen_input(write_text)
                if screen_result.blocked:
                    logger.warning(
                        "Guardian blocked memory write for %s (confidence=%.3f)",
                        tool_name,
                        screen_result.classifier_result.confidence
                        if screen_result.classifier_result
                        else 0.0,
                    )
                    results.append(
                        {
                            "tool_use_id": tool_id,
                            "content": (
                                "[Guardian] Memory write blocked — content may contain "
                                "prompt injection."
                            ),
                            "is_error": True,
                        }
                    )
                    continue

        # Execute the tool
        t0 = time.perf_counter()
        try:
            result = execute_tool_call(
                tool_name=tool_name,
                tool_input=tool_input,
                tools_config=tools_config,
                use_containers=use_containers,
                memory_manager=memory_manager,
                bridge_config=bridge_config,
                session_state=session_state,
            )
            is_error = False
            elapsed_ms = (time.perf_counter() - t0) * 1000
        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            result = f"Error: {e}"
            is_error = True
            elapsed_ms = (time.perf_counter() - t0) * 1000

        # Audit tool execution
        if guardian is not None and hasattr(guardian, "_audit") and guardian._audit:
            guardian._audit.log_tool_result(
                tool_name=tool_name,
                success=not is_error,
                duration_ms=elapsed_ms,
                output_length=len(result) if result else 0,
                error=str(result)[:200] if is_error else None,
            )

        # Screen executor output for injection
        tool_cfg = tools_config.get(tool_name)
        if (
            not is_error
            and guardian is not None
            and tool_cfg is not None
            and tool_cfg.classify_output
        ):
            screen_result = guardian.screen_tool_result(tool_name, result)
            if screen_result.blocked:
                logger.warning(
                    "Guardian blocked output from %s (confidence=%.3f)",
                    tool_name,
                    screen_result.classifier_result.confidence
                    if screen_result.classifier_result
                    else 0.0,
                )
                result = (
                    f"[Guardian] Output from '{tool_name}' was blocked by the "
                    f"security classifier. The content may contain prompt injection."
                )
                is_error = True

        # Screen search_memory results for stored injection payloads
        if not is_error and guardian is not None and tool_name == "search_memory":
            screen_result = guardian.screen_tool_result(tool_name, result)
            if screen_result.blocked:
                logger.warning(
                    "Guardian blocked search_memory output (confidence=%.3f)",
                    screen_result.classifier_result.confidence
                    if screen_result.classifier_result
                    else 0.0,
                )
                result = (
                    "[Guardian] Memory search results were blocked by the "
                    "security classifier. The stored content may contain "
                    "prompt injection."
                )
                is_error = True

        results.append(
            {
                "tool_use_id": tool_id,
                "content": result,
                "is_error": is_error,
            }
        )

    return results, None
