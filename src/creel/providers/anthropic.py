"""Anthropic provider — wraps the Anthropic SDK."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

import anthropic

from creel.providers.base import (
    ContentBlock,
    LLMAuthError,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMTransientError,
    TextBlock,
    ToolUseBlock,
    Usage,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}

# OAuth tokens (sk-ant-oat) authenticate via Claude Code's subscription path.
# The API requires these exact headers and system prompt to accept Bearer auth.
_OAUTH_HEADERS = {
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
    "user-agent": "claude-cli/2.1.59 (external, cli)",
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
        raise LLMAuthError(
            "No Anthropic credentials found. Set ANTHROPIC_AUTH_TOKEN "
            "(from `claude setup-token`) or ANTHROPIC_API_KEY in your "
            "environment, or configure secrets in the task definition.",
            status_code=401,
        )


def _convert_message(response: anthropic.types.Message) -> LLMMessage:
    """Convert an Anthropic Message to our unified LLMMessage."""
    content: list[ContentBlock] = []
    for block in response.content:
        if block.type == "text":
            content.append(TextBlock(text=block.text))
        elif block.type == "tool_use":
            content.append(
                ToolUseBlock(
                    id=block.id,
                    name=block.name,
                    input=block.input,
                )
            )

    usage = None
    if hasattr(response, "usage") and response.usage:
        usage = Usage(
            input_tokens=getattr(response.usage, "input_tokens", 0),
            output_tokens=getattr(response.usage, "output_tokens", 0),
        )

    return LLMMessage(
        content=content,
        stop_reason=response.stop_reason or "end_turn",
        usage=usage,
    )


def _wrap_api_error(exc: anthropic.APIStatusError) -> LLMProviderError:
    """Convert an Anthropic API error to a common error type."""
    code = exc.status_code
    msg = str(exc)
    if code == 429:
        return LLMRateLimitError(msg, status_code=code)
    if code in {401, 403}:
        return LLMAuthError(msg, status_code=code)
    if code in _RETRYABLE_STATUS_CODES:
        return LLMTransientError(msg, status_code=code)
    return LLMProviderError(msg, status_code=code)


class AnthropicProvider(LLMProvider):
    """LLM provider backed by the Anthropic Messages API."""

    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        """Return a cached Anthropic client, creating one on first use."""
        if self._client is None:
            self._client = _get_client()
        return self._client

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Remove empty text content blocks that would cause API 400 errors.

        The Anthropic API rejects messages containing text blocks with empty
        or whitespace-only text.  This can happen when Guardian blocks a tool
        call and synthetic error results are stored in conversation history.
        """
        sanitized: list[dict] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                cleaned = [
                    block
                    for block in content
                    if not (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and not (block.get("text") or "").strip()
                    )
                ]
                if not cleaned:
                    # All blocks were empty — replace with a placeholder so
                    # the message isn't dropped (preserves conversation flow).
                    cleaned = [{"type": "text", "text": "(empty)"}]
                sanitized.append({**msg, "content": cleaned})
            elif isinstance(content, str) and not content.strip():
                sanitized.append({**msg, "content": "(empty)"})
            else:
                sanitized.append(msg)
        return sanitized

    def create(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
        timeout: float | None = None,
    ) -> LLMMessage:
        client = self._get_client()

        resolved_system, resolved_messages = self._resolve_for_oauth(system, messages)
        resolved_messages = self._sanitize_messages(resolved_messages)

        create_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": resolved_messages,
        }

        if resolved_system:
            create_kwargs["system"] = resolved_system
        if tools:
            create_kwargs["tools"] = tools
        if timeout is not None:
            create_kwargs["timeout"] = timeout

        try:
            response = client.messages.create(**create_kwargs)
        except anthropic.APIStatusError as exc:
            raise _wrap_api_error(exc) from exc

        return _convert_message(response)

    def stream(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> LLMMessage:
        client = self._get_client()

        resolved_system, resolved_messages = self._resolve_for_oauth(system, messages)
        resolved_messages = self._sanitize_messages(resolved_messages)

        create_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": resolved_messages,
        }

        if resolved_system:
            create_kwargs["system"] = resolved_system
        if tools:
            create_kwargs["tools"] = tools

        try:
            with client.messages.stream(**create_kwargs) as stream:
                if on_text_delta is not None:
                    for text in stream.text_stream:
                        on_text_delta(text)
                return _convert_message(stream.get_final_message())
        except anthropic.APIStatusError as exc:
            raise _wrap_api_error(exc) from exc

    def health(self) -> bool:
        """Check connectivity by listing models."""
        try:
            client = self._get_client()
            client.models.list(limit=1)
            return True
        except Exception:
            return False

    def extract_env_vars(self) -> dict[str, str]:
        env: dict[str, str] = {}
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = auth_token
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        return env

    @staticmethod
    def _resolve_for_oauth(
        system: str | None,
        messages: list[dict],
    ) -> tuple[str | None, list[dict]]:
        """Handle OAuth token constraints.

        The Anthropic API requires the exact Claude Code system prefix
        (and nothing else) when authenticating with subscription-based
        OAuth tokens (sk-ant-oat).  Any custom system prompt is injected
        as a leading user message instead.
        """
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not (auth_token and _is_oauth_token(auth_token)):
            return system, messages

        if not system:
            return _CLAUDE_CODE_SYSTEM_PREFIX, messages

        # Inject custom system prompt as first user message
        preamble = {
            "role": "user",
            "content": (
                f"[IMPORTANT INSTRUCTIONS — follow these for the entire conversation]\n\n{system}"
            ),
        }
        ack = {"role": "assistant", "content": "Understood. I'll follow those instructions."}
        return _CLAUDE_CODE_SYSTEM_PREFIX, [preamble, ack, *messages]
