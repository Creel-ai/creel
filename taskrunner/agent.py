"""Agent core - the agentic loop that calls the LLM and executes tools."""

from __future__ import annotations

import logging
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
) -> AgentResult:
    """Run the agent loop: call LLM, execute tools, repeat until done.

    Args:
        messages: Conversation history + new message (Anthropic format).
        llm_config: LLM configuration.
        tools_config: Available tools.
        agent_config: Agent settings (max_turns, etc.).
        system_prompt: Optional system prompt.
        use_containers: If True, run fetchers in Docker containers.

    Returns:
        AgentResult with the final response and execution metadata.
    """
    tool_defs = build_tool_definitions(tools_config) if tools_config else []
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

            try:
                result = execute_tool_call(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tools_config=tools_config,
                    use_containers=use_containers,
                )
                is_error = False
            except Exception as e:
                logger.exception("Tool %s failed", tool_name)
                result = f"Error: {e}"
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
