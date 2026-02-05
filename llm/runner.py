#!/usr/bin/env python3
"""Containerized LLM runner.

Reads a prompt from stdin, calls the Anthropic API, and prints the response to stdout.
Configured via environment variables: ANTHROPIC_API_KEY, MODEL, MAX_TOKENS.
"""

from __future__ import annotations

import os
import sys

import anthropic


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    model = os.environ.get("MODEL", "claude-sonnet-4-20250514")
    max_tokens = int(os.environ.get("MAX_TOKENS", "300"))

    prompt = sys.stdin.read().strip()
    if not prompt:
        print("Error: No prompt provided on stdin", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    for block in message.content:
        if block.type == "text":
            print(block.text)


if __name__ == "__main__":
    main()
