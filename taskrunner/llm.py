"""LLM runner - sends prompts to Anthropic API and returns responses."""

from __future__ import annotations

import os
import subprocess
import sys

import anthropic

from taskrunner.models import LLMConfig


def run_llm(prompt: str, config: LLMConfig, use_container: bool = False) -> str:
    """Send a prompt to the LLM and return the response text.

    Args:
        prompt: The fully-rendered prompt to send.
        config: LLM configuration (model, max_tokens, secrets path).
        use_container: If True, run via Docker container. Otherwise call API directly.

    Returns:
        The LLM response text.
    """
    if use_container:
        return _run_llm_container(prompt, config)
    return _run_llm_direct(prompt, config)


def _run_llm_direct(prompt: str, config: LLMConfig) -> str:
    """Call Anthropic API directly (non-containerized)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Either set it in your environment "
            "or configure secrets in the task definition."
        )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract text from response
    text_parts = []
    for block in message.content:
        if block.type == "text":
            text_parts.append(block.text)

    return "\n".join(text_parts)


def _run_llm_container(prompt: str, config: LLMConfig) -> str:
    """Run LLM call inside an isolated Docker container."""
    env_flags = []
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        env_flags.extend(["-e", f"ANTHROPIC_API_KEY={api_key}"])

    env_flags.extend(["-e", f"MODEL={config.model}"])
    env_flags.extend(["-e", f"MAX_TOKENS={config.max_tokens}"])

    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16M",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=256m",
            "--cpus=0.5",
            *env_flags,
            "llm-runner:latest",
        ],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return result.stdout.strip()
