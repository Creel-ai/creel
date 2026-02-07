#!/usr/bin/env python3
"""Containerized LLM runner.

Reads a prompt from stdin, calls the Anthropic API, and prints the response to stdout.
Configured via environment variables: ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY, MODEL, MAX_TOKENS.
"""

from __future__ import annotations

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

    prompt = sys.stdin.read().strip()
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


if __name__ == "__main__":
    main()
