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

        create_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }

        system = self._resolve_system(system)
        if system:
            create_kwargs["system"] = system
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

        create_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }

        system = self._resolve_system(system)
        if system:
            create_kwargs["system"] = system
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
    def _resolve_system(system: str | None) -> str | None:
        """Inject Claude Code system prefix for OAuth tokens when no explicit system prompt."""
        if system:
            return system
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if auth_token and _is_oauth_token(auth_token):
            return _CLAUDE_CODE_SYSTEM_PREFIX
        return None
