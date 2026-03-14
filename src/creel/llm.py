"""LLM runner - sends prompts to LLM providers and returns responses."""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from creel.models import LLMConfig
from creel.providers import (
    LLMMessage,
    LLMRateLimitError,
    LLMTransientError,
    _resolve_model_name,
    get_provider_with_fallback,
)
from creel.rate_limiter import get_rate_limiter

if TYPE_CHECKING:
    from creel.container_pool import ContainerPool

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds

# Docker security flags for the simple LLM runner (single prompt -> response).
# Lower resource limits than the agent loop (_AGENT_DOCKER_FLAGS in container_agent.py)
# since this path doesn't run multi-turn tool loops.
_LLM_DOCKER_FLAGS = [
    "--read-only",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,size=16M",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--memory=256m",
    "--cpus=0.5",
]


def extract_text(message: LLMMessage) -> str:
    """Extract concatenated text from an LLMMessage response."""
    text_parts = []
    for block in message.content:
        if block.type == "text":
            text_parts.append(block.text)
    return "\n".join(text_parts)


def _record_usage(message: LLMMessage, model: str) -> None:
    """Record token usage from an LLM response if rate limiting is active."""
    limiter = get_rate_limiter()
    if limiter is not None and message.usage is not None:
        limiter.record(
            model=model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


def _retry_on_transient(fn, *args, **kwargs):
    """Call fn with retry on transient API errors (429/500/502/503).

    Uses exponential backoff: 1s, 2s, 4s between attempts.
    Catches the unified LLMProviderError types. Non-retryable errors
    (LLMAuthError, LLMProviderError with non-retryable status) propagate
    immediately.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except (LLMRateLimitError, LLMTransientError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "LLM call failed with %s, retrying in %.1fs (attempt %d/%d)",
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
    model_override: str | None = None,
) -> LLMMessage:
    """Call the LLM provider with multi-turn messages and optional tools.

    Args:
        messages: Conversation messages in Anthropic format.
        config: LLM configuration (provider, model, max_tokens).
        tools: Tool definitions, or None.
        system: System prompt, or None.
        on_text_delta: Optional callback invoked with each text chunk during
            streaming.  When provided, uses the streaming API instead of the
            blocking call.  Note: retry logic is not applied in streaming mode
            since a partial stream cannot be retried.
        model_override: Optional "provider/model" string that overrides
            config.model for this call (used by per-session or per-job overrides).

    Returns:
        A provider-agnostic LLMMessage object.
    """
    # Rate limit check (blocks until a slot is available or raises)
    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.check()

    effective_model = model_override or config.model

    provider = get_provider_with_fallback(
        provider=config.provider,
        model=effective_model,
        fallback=config.fallback,
        api_base=config.api_base,
        region=config.region,
    )
    model = _resolve_model_name(effective_model)

    if on_text_delta is not None:
        response = _call_llm_streaming(
            provider, model, config, messages, tools, system, on_text_delta
        )
    else:

        def _do_create():
            return provider.create(
                messages=messages,
                model=model,
                max_tokens=config.max_tokens,
                system=system,
                tools=tools,
            )

        response = _retry_on_transient(_do_create)

    _record_usage(response, effective_model)
    return response


def _call_llm_streaming(
    provider,
    model: str,
    config: LLMConfig,
    messages: list[dict],
    tools: list[dict] | None,
    system: str | None,
    on_text_delta: Callable[[str], None],
) -> LLMMessage:
    """Stream an LLM response, calling *on_text_delta* for each text chunk.

    Returns the complete LLMMessage once the stream finishes. Falls back to
    the non-streaming path on transient errors.
    """
    try:
        return provider.stream(
            messages=messages,
            model=model,
            max_tokens=config.max_tokens,
            system=system,
            tools=tools,
            on_text_delta=on_text_delta,
        )
    except (LLMRateLimitError, LLMTransientError) as exc:
        logger.warning(
            "Streaming failed with %s, falling back to non-streaming",
            exc.status_code,
        )

        def _do_create():
            return provider.create(
                messages=messages,
                model=model,
                max_tokens=config.max_tokens,
                system=system,
                tools=tools,
            )

        return _retry_on_transient(_do_create)


def summarize_messages(
    messages: list[dict],
    model: str = "claude-haiku-4-5",
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


def run_llm(
    prompt: str,
    config: LLMConfig,
    use_container: bool = False,
    container_pool: ContainerPool | None = None,
) -> str:
    """Send a prompt to the LLM and return the response text.

    Args:
        prompt: The fully-rendered prompt to send.
        config: LLM configuration (provider, model, max_tokens, secrets path).
        use_container: If True, run via Docker container. Otherwise call API directly.
        container_pool: Optional ContainerPool for warm container reuse.

    Returns:
        The LLM response text.
    """
    # Rate limit check — applies to all paths (direct, container, pooled)
    limiter = get_rate_limiter()
    if limiter is not None:
        limiter.check()

    if use_container:
        if container_pool is not None and container_pool.enabled:
            return _run_llm_pooled(prompt, config, container_pool)
        return _run_llm_container(prompt, config)
    return _run_llm_direct(prompt, config)


def _run_llm_direct(prompt: str, config: LLMConfig) -> str:
    """Call the LLM provider directly (non-containerized)."""
    provider = get_provider_with_fallback(
        provider=config.provider,
        model=config.model,
        fallback=config.fallback,
        api_base=config.api_base,
        region=config.region,
    )
    model = _resolve_model_name(config.model)

    def _do_create():
        return provider.create(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=config.max_tokens,
        )

    message = _retry_on_transient(_do_create)
    _record_usage(message, config.model)
    return extract_text(message)


def _run_llm_container(prompt: str, config: LLMConfig) -> str:
    """Run LLM call inside an isolated Docker container."""
    from creel.containers import _ensure_image

    _ensure_image("llm-runner:latest")

    from creel.container_agent import _get_llm_env_vars

    env_vars = _get_llm_env_vars(config)
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
                *_LLM_DOCKER_FLAGS,
                "--env-file",
                env_file.name,
                "llm-runner:latest",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )

    stderr = result.stderr.strip() if result.stderr else ""
    if stderr:
        if result.returncode == 0:
            logger.debug("LLM runner stderr (success):\n%s", stderr)
        else:
            logger.error("LLM runner stderr (exit %d):\n%s", result.returncode, stderr)

    if result.returncode != 0:
        error_detail = stderr[:500] if stderr else f"exit code {result.returncode}"
        raise RuntimeError(f"LLM runner failed: {error_detail}")

    return result.stdout.strip()


def _run_llm_pooled(prompt: str, config: LLMConfig, pool: ContainerPool) -> str:
    """Run LLM call using a warm container from the pool."""
    from creel.containers import _ensure_image

    _ensure_image("llm-runner:latest")

    from creel.container_agent import _get_llm_env_vars

    env_vars = _get_llm_env_vars(config)
    env_vars["MODEL"] = config.model
    env_vars["MAX_TOKENS"] = str(config.max_tokens)

    container = pool.acquire(
        image="llm-runner:latest",
        entrypoint="runner.py",
        docker_flags=_LLM_DOCKER_FLAGS,
        env_vars=env_vars,
    )

    try:
        container.send(
            {
                "type": "request",
                "prompt": prompt,
                "model": config.model,
                "max_tokens": config.max_tokens,
            }
        )
        msg = container.recv(timeout=120)

        if msg.get("type") == "response":
            pool.release(container)
            return msg["text"]
        elif msg.get("type") == "error":
            pool.release(container)
            raise RuntimeError(f"LLM container error: {msg.get('message', 'unknown')}")
        else:
            raise RuntimeError(f"Unexpected message type: {msg.get('type')}")
    except Exception:
        container.force_kill()
        with pool._lock:
            pool._remove_container(container)
        raise
