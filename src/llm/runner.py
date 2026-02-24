#!/usr/bin/env python3
"""Containerized LLM runner.

Reads a prompt from stdin, calls the Anthropic API, and prints the response to stdout.
Configured via environment variables: ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY, MODEL, MAX_TOKENS.
"""

from __future__ import annotations

import json
import os
import sys

import anthropic

_OAUTH_HEADERS = {
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
    "user-agent": "claude-cli/2.1.2 (external, cli)",
    "x-app": "cli",
}

_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def main() -> None:
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if auth_token:
        headers = _OAUTH_HEADERS if "sk-ant-oat" in auth_token else {}
        client = anthropic.Anthropic(auth_token=auth_token, default_headers=headers)
    elif api_key:
        client = anthropic.Anthropic(api_key=api_key)
    else:
        print(
            "Error: Set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY",
            file=sys.stderr,
        )
        sys.exit(1)

    model = os.environ.get("MODEL", "claude-sonnet-4-20250514")
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
            _run_keepalive(client, model, max_tokens, auth_token, msg)
            return
    except (json.JSONDecodeError, ValueError):
        pass

    # Legacy mode: first line is the start of a raw prompt.
    # Read the rest of stdin and combine. The readline()/read() split is
    # intentional for backward compatibility: the first line was already
    # consumed to detect JSON protocol mode, so we recombine it with any
    # remaining input. Single-line prompts (no trailing newline) produce
    # empty `rest`, handled by the ternary.
    rest = sys.stdin.read()
    prompt = (first_line + "\n" + rest).strip() if rest else first_line

    if not prompt:
        print("Error: No prompt provided on stdin", file=sys.stderr)
        sys.exit(1)

    create_kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if auth_token and "sk-ant-oat" in auth_token:
        create_kwargs["system"] = _CLAUDE_CODE_SYSTEM_PREFIX

    message = client.messages.create(**create_kwargs)

    for block in message.content:
        if block.type == "text":
            print(block.text)


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
    client: anthropic.Anthropic,
    model: str,
    max_tokens: int,
    auth_token: str | None,
    first_msg: dict,
) -> None:
    """Run in keepalive mode: process JSON-line requests in a loop."""

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

            create_kwargs: dict = {
                "model": req_model,
                "max_tokens": req_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if auth_token and "sk-ant-oat" in auth_token:
                create_kwargs["system"] = _CLAUDE_CODE_SYSTEM_PREFIX

            try:
                message = client.messages.create(**create_kwargs)
                text_parts = []
                for block in message.content:
                    if block.type == "text":
                        text_parts.append(block.text)
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
