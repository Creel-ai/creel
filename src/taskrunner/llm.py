"""LLM runner - sends prompts to Anthropic API and returns responses."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable

import anthropic

from taskrunner.models import LLMConfig

logger = logging.getLogger(__name__)

# Retry configuration
RETRYABLE_STATUS_CODES = {429, 500, 502, 503}
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds

_OAUTH_HEADERS = {
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
    "user-agent": "claude-cli/2.1.2 (external, cli)",
    "x-app": "cli",
}

_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def _is_oauth_token(token: str) -> bool:
    return "sk-ant-oat" in token


def _get_client() -> anthropic.Anthropic:
    """Create an Anthropic client using available credentials."""
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if auth_token:
        headers = _OAUTH_HEADERS if _is_oauth_token(auth_token) else {}
        return anthropic.Anthropic(auth_token=auth_token, default_headers=headers)
    elif api_key:
        return anthropic.Anthropic(api_key=api_key)
    else:
        raise RuntimeError(
            "No Anthropic credentials found. Set ANTHROPIC_AUTH_TOKEN "
            "(from `claude setup-token`) or ANTHROPIC_API_KEY in your "
            "environment, or configure secrets in the task definition."
        )


def extract_text(message: anthropic.types.Message) -> str:
    """Extract concatenated text from an Anthropic Message response."""
    text_parts = []
    for block in message.content:
        if block.type == "text":
            text_parts.append(block.text)
    return "\n".join(text_parts)


def _retry_on_transient(fn, *args, **kwargs):
    """Call fn with retry on transient API errors (429/500/502/503).

    Uses exponential backoff: 1s, 2s, 4s between attempts.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except anthropic.APIStatusError as exc:
            if exc.status_code not in RETRYABLE_STATUS_CODES:
                raise
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "LLM call failed with %d, retrying in %.1fs (attempt %d/%d)",
                    exc.status_code,
                    delay,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def call_llm(
    messages: list[dict],
    config: LLMConfig,
    tools: list[dict] | None = None,
    system: str | None = None,
    on_text_delta: Callable[[str], None] | None = None,
) -> anthropic.types.Message:
    """Call the Anthropic API with multi-turn messages and optional tools.

    Args:
        messages: Conversation messages in Anthropic format.
        config: LLM configuration (model, max_tokens).
        tools: Anthropic tool definitions, or None.
        system: System prompt, or None.
        on_text_delta: Optional callback invoked with each text chunk during
            streaming.  When provided, uses the streaming API instead of the
            blocking ``messages.create`` call.  Note: retry logic is not applied
            in streaming mode since a partial stream cannot be retried.

    Returns:
        The raw Anthropic Message object.
    """
    client = _get_client()

    create_kwargs: dict = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "messages": messages,
    }

    # System prompt: use explicit if provided, else OAuth prefix if needed
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if system:
        create_kwargs["system"] = system
    elif auth_token and _is_oauth_token(auth_token):
        create_kwargs["system"] = _CLAUDE_CODE_SYSTEM_PREFIX

    if tools:
        create_kwargs["tools"] = tools

    if on_text_delta is not None:
        return _call_llm_streaming(client, create_kwargs, on_text_delta)

    return _retry_on_transient(client.messages.create, **create_kwargs)


def _call_llm_streaming(
    client: anthropic.Anthropic,
    create_kwargs: dict,
    on_text_delta: Callable[[str], None],
) -> anthropic.types.Message:
    """Stream an LLM response, calling *on_text_delta* for each text chunk.

    Returns the complete ``Message`` once the stream finishes — callers get
    the same type as the non-streaming path.

    Falls back to the non-streaming path on transient API errors so the
    caller still gets a result (at the cost of losing incremental output).
    """
    try:
        with client.messages.stream(**create_kwargs) as stream:
            for text in stream.text_stream:
                on_text_delta(text)
            return stream.get_final_message()
    except anthropic.APIStatusError as exc:
        if exc.status_code not in RETRYABLE_STATUS_CODES:
            raise
        logger.warning(
            "Streaming failed with %d, falling back to non-streaming",
            exc.status_code,
        )
        return _retry_on_transient(client.messages.create, **create_kwargs)


def summarize_messages(
    messages: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
    use_container: bool = False,
) -> str:
    """Summarize a list of conversation messages into a compact context string.

    Args:
        messages: Conversation messages in Anthropic format.
        model: Model to use for summarization.
        max_tokens: Max output tokens for the summary.
        use_container: If True, run the summarization inside a Docker container.

    Returns:
        A summary string covering key topics, decisions, tool outcomes, and pending items.
    """
    # Format messages into human-readable text
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"{role}: {content}")
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        parts.append(
                            f"[tool_use: {block.get('name', '?')}({block.get('input', {})})]"
                        )
                    elif block.get("type") == "tool_result":
                        result_text = str(block.get("content", ""))
                        if len(result_text) > 200:
                            result_text = result_text[:200] + "..."
                        parts.append(f"[tool_result: {result_text}]")
            lines.append(f"{role}: {' '.join(parts)}")

    conversation_text = "\n".join(lines)

    prompt = (
        "Summarize the following conversation concisely. Focus on:\n"
        "- Key topics discussed\n"
        "- Decisions made\n"
        "- Tool call outcomes and important results\n"
        "- Any pending items or unresolved questions\n\n"
        "Keep the summary compact but preserve all important context needed "
        "to continue the conversation.\n\n"
        f"Conversation:\n{conversation_text}"
    )

    config = LLMConfig(model=model, max_tokens=max_tokens)
    return run_llm(prompt, config, use_container=use_container)


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
    client = _get_client()

    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    create_kwargs: dict = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if auth_token and _is_oauth_token(auth_token):
        create_kwargs["system"] = _CLAUDE_CODE_SYSTEM_PREFIX

    message = _retry_on_transient(client.messages.create, **create_kwargs)
    return extract_text(message)


def _run_llm_container(prompt: str, config: LLMConfig) -> str:
    """Run LLM call inside an isolated Docker container."""
    from taskrunner.orchestrator import _ensure_image

    _ensure_image("llm-runner:latest")

    env_vars: dict[str, str] = {}
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if auth_token:
        env_vars["ANTHROPIC_AUTH_TOKEN"] = auth_token
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        env_vars["ANTHROPIC_API_KEY"] = api_key

    env_vars["MODEL"] = config.model
    env_vars["MAX_TOKENS"] = str(config.max_tokens)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=True, prefix="creel-"
    ) as env_file:
        for key, value in env_vars.items():
            env_file.write(f"{key}={value}\n")
        env_file.flush()

        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16M",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory=256m",
                "--cpus=0.5",
                "--env-file",
                env_file.name,
                "llm-runner:latest",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    return result.stdout.strip()
