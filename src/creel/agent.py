"""Agent core - the agentic loop that calls the LLM and executes tools."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from creel.llm import call_llm, extract_text
from creel.models import AgentConfig, BridgeConfig, LLMConfig, SessionState, ToolConfig
from creel.tools import build_tool_definitions, execute_tool_call

logger = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    """Info about a tool call that needs async approval."""

    tool_name: str
    tool_input: dict
    reason: str
    tool_use_id: str = ""


@dataclass
class AgentResult:
    """Result of an agent loop execution."""

    text: str
    turns_used: int
    tool_calls_made: int
    stop_reason: str  # "end_turn" | "max_turns" | "error" | "approval_required"
    tool_history: list[dict] = field(default_factory=list)
    pending_approval: PendingApproval | None = None
    last_input_tokens: int = 0


def _extract_prior_tools(messages: list[dict]) -> list[str]:
    """Extract tool names from prior assistant messages for coherence context."""
    prior = []
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        prior.append(block.get("name", ""))
    return prior


_RETRY_PHRASES = frozenset(
    {
        "try again",
        "try that again",
        "retry",
        "do that again",
        "run that again",
        "one more time",
        "redo",
        "again please",
        "can you retry",
        "please retry",
        "try it again",
    }
)


def _extract_user_request_for_coherence(messages: list[dict]) -> str:
    """Extract the user request for coherence checking.

    If the latest user message is a short retry phrase (e.g. "try that again"),
    prepend the previous substantive user message so the coherence checker
    understands what "that" refers to.
    """
    user_messages: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        else:
            continue
        if text.strip():
            user_messages.append(text.strip())

    if not user_messages:
        return ""

    latest = user_messages[-1]

    # Check if the latest message is a short retry phrase
    normalised = latest.lower().rstrip("!?.").strip()
    is_retry = normalised in _RETRY_PHRASES or any(normalised.startswith(p) for p in _RETRY_PHRASES)

    if is_retry and len(user_messages) >= 2:
        prior = user_messages[-2]
        return f"{prior}\n\n(Follow-up: user said '{latest}', meaning retry the above)"

    return latest


_MEMORY_WRITE_TOOLS = frozenset({"remember", "update_long_term_memory", "edit_memory"})


@dataclass
class _GuardianDecision:
    """Result of pre-execution Guardian checks."""

    action: str  # "allow" | "deny" | "needs_approval"
    error_message: str = ""


def _check_guardian_pre_execution(
    guardian: Any,
    tool_name: str,
    tool_input: dict,
    messages: list[dict],
    tools_config: dict[str, ToolConfig],
    confirm_action: Callable[[str, dict, str], bool] | None,
) -> _GuardianDecision:
    """Run all Guardian pre-execution checks for a single tool call.

    Checks (in order): policy validation, user confirmation for REVIEW
    verdicts, coherence verification, and memory write screening.

    Returns a _GuardianDecision indicating whether to proceed, deny, or
    request async approval.
    """
    from guardian.types import ActionVerdict

    # Stage 1: Policy validation
    decision = guardian.validate_action(tool_name, tool_input)
    if decision.verdict == ActionVerdict.DENY:
        logger.warning("Guardian denied tool %s: %s", tool_name, decision.reason)
        guardian.log_action_outcome(tool_name, "deny", "denied_by_policy")
        return _GuardianDecision(
            action="deny",
            error_message=f"Action denied by security policy: {decision.reason}",
        )

    if decision.verdict == ActionVerdict.REVIEW:
        logger.warning("Guardian review for tool %s: %s", tool_name, decision.reason)
        if confirm_action is not None:
            if not confirm_action(tool_name, tool_input, decision.reason):
                logger.info("User denied tool %s during review", tool_name)
                guardian.log_action_outcome(tool_name, "review", "denied_by_user")
                return _GuardianDecision(
                    action="deny",
                    error_message=f"Action denied by user: {decision.reason}",
                )
            guardian.log_action_outcome(tool_name, "review", "approved_by_user")
        else:
            # No synchronous callback — signal that async approval is needed.
            # The caller handles injecting synthetic results and returning.
            return _GuardianDecision(
                action="needs_approval",
                error_message=decision.reason,
            )

    # Stage 2: Coherence check
    user_request = _extract_last_user_text(messages)
    if user_request:
        prior_tools = _extract_prior_tools(messages)
        available_tool_names = list(tools_config.keys()) if tools_config else None
        coherence = guardian.check_coherence(
            user_request,
            tool_name,
            tool_input,
            prior_tools=prior_tools,
            available_tools=available_tool_names,
        )
        if not coherence.coherent:
            logger.warning(
                "Guardian coherence check failed for %s: %s",
                tool_name,
                coherence.reasoning,
            )
            return _GuardianDecision(
                action="deny",
                error_message=f"Action blocked — not coherent with user request: {coherence.reasoning}",
            )

    # Stage 3: Memory write screening
    if tool_name in _MEMORY_WRITE_TOOLS:
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
                return _GuardianDecision(
                    action="deny",
                    error_message=(
                        "[Guardian] Memory write blocked — content may contain "
                        "prompt injection."
                    ),
                )

    return _GuardianDecision(action="allow")


def _extract_last_user_text(messages: list[dict]) -> str:
    """Extract the text from the last user message in the conversation."""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    str(block_item.get("text", ""))
                    for block_item in content
                    if isinstance(block_item, dict) and block_item.get("type") == "text"
                )
            break
    return ""


def _record_tool_error(
    tool_use_id: str,
    tool_name: str,
    tool_input: dict,
    message: str,
    tool_history: list[dict],
    tool_results: list[dict],
) -> None:
    """Record a tool-call error into both tracking lists.

    Used when a tool call is blocked before execution (e.g. by policy,
    coherence check, or Guardian screening).
    """
    tool_history.append(
        {
            "tool": tool_name,
            "input": tool_input,
            "output": message,
            "is_error": True,
        }
    )
    tool_results.append(
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": message,
            "is_error": True,
        }
    )


def _ensure_tool_call_integrity(messages: list[dict]) -> int:
    """Repair orphaned tool_use/tool_result sequences in-place.

    Anthropic requires every assistant ``tool_use`` block to be followed
    immediately by a user message containing matching ``tool_result`` blocks.
    If the pair is broken (for example, by an interrupted approval flow),
    inject synthetic ``tool_result`` blocks so later LLM calls remain valid.

    Returns the number of synthetic tool_result blocks inserted.
    """
    inserted_blocks = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role")
        content = msg.get("content")
        if role != "assistant" or not isinstance(content, list):
            i += 1
            continue

        tool_uses = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        ]
        if not tool_uses:
            i += 1
            continue

        required_ids = [str(block["id"]) for block in tool_uses]
        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        next_role = next_msg.get("role") if isinstance(next_msg, dict) else None
        next_content = next_msg.get("content") if isinstance(next_msg, dict) else None
        valid_next_list = next_role == "user" and isinstance(next_content, list)

        if valid_next_list:
            assert isinstance(next_content, list)
            tool_result_blocks = [
                block
                for block in next_content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            contains_non_tool_results = any(
                not isinstance(block, dict) or block.get("type") != "tool_result"
                for block in next_content
            )
            present_ids = {
                str(block.get("tool_use_id"))
                for block in tool_result_blocks
                if block.get("tool_use_id") is not None
            }

            missing_ids = [tool_id for tool_id in required_ids if tool_id not in present_ids]
            if not missing_ids:
                i += 2
                continue

            if not contains_non_tool_results:
                # Pure tool_result message — append missing results in-place.
                for tool_id in missing_ids:
                    next_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": (
                                "Tool execution was blocked or interrupted before results "
                                "were recorded."
                            ),
                            "is_error": True,
                        }
                    )
                inserted_blocks += len(missing_ids)
                i += 2
                continue

            # Mixed content (text + tool_results) — insert a separate message
            # with only the missing results so we don't duplicate existing ones.
            synthetic_for_missing = [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": (
                        "Tool execution was blocked or interrupted before results were recorded."
                    ),
                    "is_error": True,
                }
                for tool_id in missing_ids
            ]
            messages.insert(i + 1, {"role": "user", "content": synthetic_for_missing})
            inserted_blocks += len(synthetic_for_missing)
            i += 3  # skip: assistant, inserted synthetic, original next_msg
            continue

        synthetic_results = [
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": (
                    "Tool execution was blocked or interrupted before results were recorded."
                ),
                "is_error": True,
            }
            for tool_id in required_ids
        ]
        messages.insert(i + 1, {"role": "user", "content": synthetic_results})
        inserted_blocks += len(synthetic_results)
        i += 2

    if inserted_blocks:
        logger.warning(
            "Repaired %d orphaned tool_result block(s) in message history",
            inserted_blocks,
        )
    return inserted_blocks


def run_agent_loop(
    messages: list[dict],
    llm_config: LLMConfig,
    tools_config: dict[str, ToolConfig],
    agent_config: AgentConfig,
    system_prompt: str | None = None,
    use_containers: bool = False,
    guardian: Any | None = None,
    confirm_action: Callable[[str, dict, str], bool] | None = None,
    memory_manager: Any | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    allowed_tools: list[str] | None = None,
    bridge_config: BridgeConfig | None = None,
    session_state: SessionState | None = None,
    cron_manager: object | None = None,
    subagent_manager: object | None = None,
) -> AgentResult:
    """Run the agent loop: call LLM, execute tools, repeat until done.

    Args:
        messages: Conversation history + new message (Anthropic format).
        llm_config: LLM configuration.
        tools_config: Available tools.
        agent_config: Agent settings (max_turns, etc.).
        system_prompt: Optional system prompt.
        use_containers: If True, run executors in Docker containers.
        guardian: Optional Guardian instance for action validation.
        on_text_delta: Optional streaming callback passed to call_llm().
        allowed_tools: Optional per-task tool whitelist. If set, only these
            tools may be called regardless of global policy.
        cron_manager: Optional CronManager for cron scheduling tool.
        subagent_manager: Optional SubAgentManager for sub-agent tool.

    Returns:
        AgentResult with the final response and execution metadata.
    """
    include_memory = memory_manager is not None
    include_workspace = bool(
        tools_config and any(tc.executor == "file_ops" for tc in tools_config.values())
    )
    tool_defs = (
        build_tool_definitions(
            tools_config,
            include_memory_tools=include_memory,
            include_workspace_tools=include_workspace,
            include_cron_tools=cron_manager is not None,
            include_subagent_tool=subagent_manager is not None,
        )
        if tools_config
        else []
    )
    turns_used = 0
    tool_calls_made = 0
    tool_history: list[dict] = []
    last_input_tokens = 0

    for _turn in range(agent_config.max_turns):
        turns_used += 1
        logger.info("Agent turn %d/%d", turns_used, agent_config.max_turns)
        _ensure_tool_call_integrity(messages)

        try:
            response = call_llm(
                messages=messages,
                config=llm_config,
                tools=tool_defs if tool_defs else None,
                system=system_prompt,
                on_text_delta=on_text_delta,
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

        # Track token usage from response
        if hasattr(response, "usage") and response.usage:
            last_input_tokens = getattr(response.usage, "input_tokens", 0)

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
                last_input_tokens=last_input_tokens,
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

            # Per-task tool scoping — reject tools not in the whitelist
            if allowed_tools is not None and tool_name not in allowed_tools:
                logger.warning(
                    "Tool %s blocked by per-task scope (allowed: %s)",
                    tool_name,
                    allowed_tools,
                )
                _record_tool_error(
                    block.id,
                    tool_name,
                    tool_input,
                    f"Tool '{tool_name}' is not permitted for this task. "
                    f"Allowed tools: {', '.join(allowed_tools)}",
                    tool_history,
                    tool_results,
                )
                continue

            # Guardian pre-execution checks (policy, coherence, memory screening)
            if guardian is not None:
                gd = _check_guardian_pre_execution(
                    guardian, tool_name, tool_input, messages, tools_config, confirm_action,
                )
                if gd.action == "deny":
                    _record_tool_error(
                        block.id, tool_name, tool_input,
                        gd.error_message, tool_history, tool_results,
                    )
                    continue
                if gd.action == "needs_approval":
                    # No synchronous callback — inject synthetic tool_results
                    # for ALL tool_use blocks and return for async approval.
                    synthetic_results = []
                    for b in tool_use_blocks:
                        if b.id == block.id:
                            approval_msg = f"Action requires approval: {gd.error_message}"
                        else:
                            approval_msg = (
                                "Action skipped — another tool in this batch requires approval."
                            )
                        synthetic_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": b.id,
                                "content": approval_msg,
                                "is_error": True,
                            }
                        )
                    messages.append({"role": "user", "content": synthetic_results})
                    return AgentResult(
                        text="This action requires approval before proceeding.",
                        turns_used=turns_used,
                        tool_calls_made=tool_calls_made,
                        stop_reason="approval_required",
                        tool_history=tool_history,
                        pending_approval=PendingApproval(
                            tool_name=tool_name,
                            tool_input=tool_input,
                            reason=gd.error_message,
                            tool_use_id=block.id,
                        ),
                        last_input_tokens=last_input_tokens,
                    )

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
                    cron_manager=cron_manager,
                    subagent_manager=subagent_manager,
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

            # Drift detection — check for behavioral anomalies
            if guardian is not None:
                drift_alerts = guardian.check_drift(
                    tool_name=tool_name,
                    output_length=len(result) if result else 0,
                    success=not is_error,
                )
                for alert in drift_alerts:
                    logger.warning(
                        "Drift alert (%s): %s",
                        alert.alert_type,
                        alert.detail,
                    )

            # Classify executor output for tools that return untrusted content
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

            # Post-execution credential scanning — redact leaked secrets
            if not is_error and guardian is not None:
                from guardian.credential_scanner import redact_credentials

                redacted_result, cred_matches = redact_credentials(result)
                if cred_matches:
                    logger.warning(
                        "Credential leak detected in %s output: %d pattern(s)",
                        tool_name,
                        len(cred_matches),
                    )
                    # Log to audit using already-detected matches (no re-scan)
                    if hasattr(guardian, "_audit") and guardian._audit:
                        guardian._audit.log_credential_leak(
                            tool_name=tool_name,
                            patterns_found=[
                                {"pattern": m.pattern_name, "redacted": m.matched_text}
                                for m in cred_matches
                            ],
                            count=len(cred_matches),
                        )
                    result = redacted_result

            tool_history.append(
                {
                    "tool": tool_name,
                    "input": tool_input,
                    "output": result,
                    "is_error": is_error,
                }
            )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": is_error,
                }
            )

        # Append tool results as a user message
        messages.append({"role": "user", "content": tool_results})

    # Max turns reached - do a final call without tools to force a summary.
    # Don't stream the forced summary — it's an internal wrap-up, not a direct
    # response, and streaming it would concatenate with the prior streamed output.
    logger.warning("Max turns (%d) reached, forcing final response", agent_config.max_turns)
    _ensure_tool_call_integrity(messages)
    try:
        response = call_llm(
            messages=messages,
            config=llm_config,
            tools=None,
            system=system_prompt,
        )
        text = extract_text(response)
        messages.append({"role": "assistant", "content": _serialize_content(response.content)})
        if hasattr(response, "usage") and response.usage:
            last_input_tokens = getattr(response.usage, "input_tokens", 0)
    except Exception as e:
        logger.exception("Final LLM call failed")
        text = f"Error on final turn: {e}"

    return AgentResult(
        text=text,
        turns_used=turns_used,
        tool_calls_made=tool_calls_made,
        stop_reason="max_turns",
        tool_history=tool_history,
        last_input_tokens=last_input_tokens,
    )


def _serialize_content(content: list) -> list[dict]:
    """Serialize Anthropic content blocks to dicts for message history."""
    serialized = []
    for block in content:
        if block.type == "text":
            serialized.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            serialized.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return serialized
