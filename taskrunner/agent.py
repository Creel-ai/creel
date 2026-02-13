"""Agent core - the agentic loop that calls the LLM and executes tools."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from taskrunner.llm import call_llm, extract_text
from taskrunner.models import AgentConfig, LLMConfig, ToolConfig
from taskrunner.tools import build_tool_definitions, execute_tool_call

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Result of an agent loop execution."""

    text: str
    turns_used: int
    tool_calls_made: int
    stop_reason: str  # "end_turn" | "max_turns" | "error"
    tool_history: list[dict] = field(default_factory=list)


def run_agent_loop(
    messages: list[dict],
    llm_config: LLMConfig,
    tools_config: dict[str, ToolConfig],
    agent_config: AgentConfig,
    system_prompt: str | None = None,
    use_containers: bool = False,
    guardian: object | None = None,
    confirm_action: Callable[[str, dict, str], bool] | None = None,
    memory_manager: object | None = None,
) -> AgentResult:
    """Run the agent loop: call LLM, execute tools, repeat until done.

    Args:
        messages: Conversation history + new message (Anthropic format).
        llm_config: LLM configuration.
        tools_config: Available tools.
        agent_config: Agent settings (max_turns, etc.).
        system_prompt: Optional system prompt.
        use_containers: If True, run fetchers in Docker containers.
        guardian: Optional Guardian instance for action validation.
        confirm_action: Optional callback for REVIEW verdicts. Takes
            (tool_name, tool_input, reason) and returns True to proceed
            or False to deny. If None, REVIEW logs and proceeds.

    Returns:
        AgentResult with the final response and execution metadata.
    """
    include_memory = memory_manager is not None
    tool_defs = build_tool_definitions(tools_config, include_memory_tools=include_memory) if tools_config else []
    turns_used = 0
    tool_calls_made = 0
    tool_history: list[dict] = []

    for turn in range(agent_config.max_turns):
        turns_used += 1
        logger.info("Agent turn %d/%d", turns_used, agent_config.max_turns)

        try:
            response = call_llm(
                messages=messages,
                config=llm_config,
                tools=tool_defs if tool_defs else None,
                system=system_prompt,
            )
        except Exception as e:
            logger.exception("LLM call failed on turn %d", turns_used)
            return AgentResult(
                text=f"Error calling LLM: {e}",
                turns_used=turns_used,
                tool_calls_made=tool_calls_made,
                stop_reason="error",
                tool_history=tool_history,
            )

        # Check for tool_use blocks
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            # No tools called - we have a final text response
            text = extract_text(response)
            # Append assistant response to messages for session tracking
            messages.append({"role": "assistant", "content": _serialize_content(response.content)})
            return AgentResult(
                text=text,
                turns_used=turns_used,
                tool_calls_made=tool_calls_made,
                stop_reason="end_turn",
                tool_history=tool_history,
            )

        # Append the assistant message (with tool_use blocks) to history
        messages.append({"role": "assistant", "content": _serialize_content(response.content)})

        # Execute each tool call and collect results
        tool_results = []
        for block in tool_use_blocks:
            tool_calls_made += 1
            tool_name = block.name
            tool_input = block.input
            logger.info("Tool call: %s(%s)", tool_name, tool_input)

            # Guardian action validation (stage 3)
            if guardian is not None:
                from guardian.types import ActionVerdict

                decision = guardian.validate_action(tool_name, tool_input)
                if decision.verdict == ActionVerdict.DENY:
                    logger.warning("Guardian denied tool %s: %s", tool_name, decision.reason)
                    guardian.log_action_outcome(tool_name, "deny", "denied_by_policy")
                    result = f"Action denied by security policy: {decision.reason}"
                    is_error = True

                    tool_history.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "output": result,
                        "is_error": is_error,
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                        "is_error": is_error,
                    })
                    continue

                if decision.verdict == ActionVerdict.REVIEW:
                    logger.warning("Guardian review for tool %s: %s", tool_name, decision.reason)
                    approved = confirm_action(tool_name, tool_input, decision.reason) if confirm_action is not None else False
                    if not approved:
                        deny_source = "user" if confirm_action is not None else "policy (no confirm handler — fail-closed)"
                        logger.info("Tool %s denied during review by %s", tool_name, deny_source)
                        if hasattr(guardian, 'log_action_outcome'):
                            guardian.log_action_outcome(tool_name, "review", f"denied_by_{deny_source}")
                        result = f"Action denied by {deny_source}: {decision.reason}"
                        is_error = True

                        tool_history.append({
                            "tool": tool_name,
                            "input": tool_input,
                            "output": result,
                            "is_error": is_error,
                        })
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                            "is_error": is_error,
                        })
                        continue
                    if hasattr(guardian, 'log_action_outcome'):
                        guardian.log_action_outcome(tool_name, "review", "approved")

            t0 = time.perf_counter()
            try:
                result = execute_tool_call(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tools_config=tools_config,
                    use_containers=use_containers,
                    memory_manager=memory_manager,
                )
                is_error = False
                elapsed_ms = (time.perf_counter() - t0) * 1000
            except Exception as e:
                logger.exception("Tool %s failed", tool_name)
                result = f"Error: {e}"
                is_error = True
                elapsed_ms = (time.perf_counter() - t0) * 1000

            # Audit tool execution result
            if guardian is not None and hasattr(guardian, "_audit") and guardian._audit:
                guardian._audit.log_tool_result(
                    tool_name=tool_name,
                    success=not is_error,
                    duration_ms=elapsed_ms,
                    output_length=len(result) if result else 0,
                    error=str(result)[:200] if is_error else None,
                )

            # Classify fetcher output for tools that return untrusted content
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

            tool_history.append({
                "tool": tool_name,
                "input": tool_input,
                "output": result,
                "is_error": is_error,
            })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
                "is_error": is_error,
            })

        # Append tool results as a user message
        messages.append({"role": "user", "content": tool_results})

    # Max turns reached - do a final call without tools to force a summary
    logger.warning("Max turns (%d) reached, forcing final response", agent_config.max_turns)
    try:
        response = call_llm(
            messages=messages,
            config=llm_config,
            tools=None,
            system=system_prompt,
        )
        text = extract_text(response)
        messages.append({"role": "assistant", "content": _serialize_content(response.content)})
    except Exception as e:
        logger.exception("Final LLM call failed")
        text = f"Error on final turn: {e}"

    return AgentResult(
        text=text,
        turns_used=turns_used,
        tool_calls_made=tool_calls_made,
        stop_reason="max_turns",
        tool_history=tool_history,
    )


def _serialize_content(content: list) -> list[dict]:
    """Serialize Anthropic content blocks to dicts for message history."""
    serialized = []
    for block in content:
        if block.type == "text":
            serialized.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            serialized.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return serialized
