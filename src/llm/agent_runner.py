#!/usr/bin/env python3
"""Containerized agent loop runner.

Runs inside a Docker container with LLM provider credentials.
Communicates with the host orchestrator via JSON-over-stdio:

  Host -> Container (stdin):
    {"type": "start", "messages": [...], "tools": [...], "system": "...",
     "model": "...", "max_tokens": 1024, "max_turns": 15}
    {"type": "tool_results", "results": [{"tool_use_id": "...", "content": "...", "is_error": false}]}

  Container -> Host (stdout):
    {"type": "tool_request", "calls": [{"id": "...", "name": "...", "input": {...}}]}
    {"type": "final", "text": "...", "turns_used": N, "tool_calls_made": N,
     "stop_reason": "end_turn", "tool_history": [...]}
    {"type": "error", "message": "..."}

No imports from creel/. Only depends on the provider's SDK.
"""

from __future__ import annotations

import json
import os
import sys

# Ensure _provider is importable whether we're running as a script inside
# the container (/app/) or imported as a module on the host (src/llm/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _provider import ContainerLLMResponse, ContainerProvider, get_container_provider  # noqa: E402


def _send(obj: dict) -> None:
    """Write a JSON line to stdout."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _recv() -> dict:
    """Read a JSON line from stdin."""
    line = sys.stdin.readline()
    if not line:
        raise EOFError("stdin closed")
    return json.loads(line)


def _extract_text(response: ContainerLLMResponse) -> str:
    return "\n".join(b["text"] for b in response.content if b.get("type") == "text")


def _run_session(provider: ContainerProvider, start: dict) -> None:
    """Run a single agent session from a 'start' message to 'final'."""
    messages: list[dict] = start["messages"]
    tools: list[dict] = start.get("tools", [])
    system_prompt: str | None = start.get("system")
    model: str = start.get("model", "claude-sonnet-4-20250514")
    max_tokens: int = start.get("max_tokens", 1024)
    max_turns: int = start.get("max_turns", 15)

    turns_used = 0
    tool_calls_made = 0
    tool_history: list[dict] = []
    last_input_tokens = 0

    for _turn in range(max_turns):
        turns_used += 1

        try:
            response = provider.create(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                tools=tools or None,
            )
        except Exception as e:
            _send({"type": "error", "message": f"LLM call failed: {e}"})
            return

        last_input_tokens = response.input_tokens

        tool_use_blocks = [b for b in response.content if b.get("type") == "tool_use"]

        if not tool_use_blocks:
            # Final text response
            text = _extract_text(response)
            messages.append({"role": "assistant", "content": response.content})
            _send(
                {
                    "type": "final",
                    "text": text,
                    "turns_used": turns_used,
                    "tool_calls_made": tool_calls_made,
                    "stop_reason": "end_turn",
                    "tool_history": tool_history,
                    "last_input_tokens": last_input_tokens,
                    "messages": messages,
                }
            )
            return

        # Tool calls - send request to host
        messages.append({"role": "assistant", "content": response.content})

        calls = []
        for block in tool_use_blocks:
            tool_calls_made += 1
            calls.append(
                {
                    "id": block["id"],
                    "name": block["name"],
                    "input": block.get("input", {}),
                }
            )

        _send({"type": "tool_request", "calls": calls})

        # Read tool results from host
        try:
            results_msg = _recv()
        except EOFError:
            _send(
                {
                    "type": "error",
                    "message": "Host closed stdin while waiting for tool results",
                }
            )
            return

        if results_msg.get("type") != "tool_results":
            _send(
                {
                    "type": "error",
                    "message": f"Expected 'tool_results', got '{results_msg.get('type')}'",
                }
            )
            return

        # Build tool result message
        tool_results: list[dict] = []
        for r in results_msg["results"]:
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": r["tool_use_id"],
                    "content": r["content"],
                    "is_error": r.get("is_error", False),
                }
            )
            tool_history.append(
                {
                    "tool": next(
                        (c["name"] for c in calls if c["id"] == r["tool_use_id"]),
                        "unknown",
                    ),
                    "input": next((c["input"] for c in calls if c["id"] == r["tool_use_id"]), {}),
                    "output": r["content"],
                    "is_error": r.get("is_error", False),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    # Max turns reached - force a final response without tools
    try:
        response = provider.create(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
        )
        text = _extract_text(response)
        last_input_tokens = response.input_tokens
    except Exception as e:
        text = f"Error on final turn: {e}"

    messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    _send(
        {
            "type": "final",
            "text": text,
            "turns_used": turns_used,
            "tool_calls_made": tool_calls_made,
            "stop_reason": "max_turns",
            "tool_history": tool_history,
            "last_input_tokens": last_input_tokens,
            "messages": messages,
        }
    )


def main() -> None:
    try:
        provider = get_container_provider()
    except (RuntimeError, ImportError, ValueError) as e:
        _send({"type": "error", "message": str(e)})
        sys.exit(1)

    # Outer loop: handle multiple sessions (warm container keepalive)
    while True:
        try:
            msg = _recv()
        except EOFError:
            # stdin closed — host is done with us
            break

        msg_type = msg.get("type")

        if msg_type == "ping":
            _send({"type": "pong"})
            continue

        if msg_type == "shutdown":
            break

        if msg_type == "reset":
            # Clear any per-session state (none currently held at module level)
            _send({"type": "ready"})
            continue

        if msg_type == "start":
            _run_session(provider, msg)
            continue

        # Unknown message type — send an error but keep looping so a single
        # bad message doesn't tear down a warm container.
        _send({"type": "error", "message": f"Expected 'start', got '{msg_type}'"})


if __name__ == "__main__":
    main()
