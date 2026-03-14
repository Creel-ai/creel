#!/usr/bin/env python3
"""Containerized LLM runner.

Reads a prompt from stdin, calls the configured LLM provider, and prints
the response to stdout.  Configured via environment variables:

- ``PROVIDER``: LLM provider name (anthropic, openai, bedrock, ollama).
  Defaults to ``anthropic``.
- ``MODEL``, ``MAX_TOKENS``: model identifier and token limit.
- Provider-specific credentials (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
"""

from __future__ import annotations

import json
import os
import sys

# Ensure _provider is importable whether we're running as a script inside
# the container (/app/) or imported as a module on the host (src/llm/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _provider import (  # noqa: E402
    CLAUDE_CODE_SYSTEM_PREFIX,
    AnthropicContainerProvider,
    ContainerProvider,
    get_container_provider,
)


def main() -> None:
    try:
        provider = get_container_provider()
    except (RuntimeError, ImportError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    model = os.environ.get("MODEL", "claude-sonnet-4-6")
    max_tokens = int(os.environ.get("MAX_TOKENS", "300"))

    # Try to read a JSON line first (keepalive protocol).
    # Fall back to raw stdin read for backward compatibility.
    first_line = sys.stdin.readline()
    if not first_line:
        print("Error: No input provided on stdin", file=sys.stderr)
        sys.exit(1)

    first_line = first_line.strip()

    # Detect JSON protocol mode
    try:
        msg = json.loads(first_line)
        if isinstance(msg, dict) and "type" in msg:
            _run_keepalive(provider, model, max_tokens, msg)
            return
    except (json.JSONDecodeError, ValueError):
        pass

    # Legacy mode: first line is the start of a raw prompt.
    rest = sys.stdin.read()
    prompt = (first_line + "\n" + rest).strip() if rest else first_line

    if not prompt:
        print("Error: No prompt provided on stdin", file=sys.stderr)
        sys.exit(1)

    system = None
    if isinstance(provider, AnthropicContainerProvider) and provider.uses_oauth:
        system = CLAUDE_CODE_SYSTEM_PREFIX

    response = provider.create(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        max_tokens=max_tokens,
        system=system,
    )

    for block in response.content:
        if block.get("type") == "text":
            print(block["text"])


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


def _run_keepalive(
    provider: ContainerProvider,
    model: str,
    max_tokens: int,
    first_msg: dict,
) -> None:
    """Run in keepalive mode: process JSON-line requests in a loop."""
    # Determine if we should inject the OAuth system prefix
    inject_system = isinstance(provider, AnthropicContainerProvider) and provider.uses_oauth

    def _handle(msg: dict) -> bool:
        """Handle a single message. Returns False to exit."""
        msg_type = msg.get("type")

        if msg_type == "ping":
            _send({"type": "pong"})
            return True

        if msg_type == "shutdown":
            return False

        if msg_type == "reset":
            _send({"type": "ready"})
            return True

        if msg_type == "request":
            prompt = msg.get("prompt", "")
            req_model = msg.get("model", model)
            req_max_tokens = msg.get("max_tokens", max_tokens)

            system = CLAUDE_CODE_SYSTEM_PREFIX if inject_system else None

            try:
                response = provider.create(
                    messages=[{"role": "user", "content": prompt}],
                    model=req_model,
                    max_tokens=req_max_tokens,
                    system=system,
                )
                text_parts = []
                for block in response.content:
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                _send({"type": "response", "text": "\n".join(text_parts)})
            except Exception as e:
                _send({"type": "error", "message": str(e)})
            return True

        _send({"type": "error", "message": f"Unknown message type: {msg_type}"})
        return True

    # Handle the first message that was already read
    if not _handle(first_msg):
        return

    # Continue reading messages
    while True:
        try:
            msg = _recv()
        except EOFError:
            break
        if not _handle(msg):
            break


if __name__ == "__main__":
    main()
