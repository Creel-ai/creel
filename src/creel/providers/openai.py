"""OpenAI provider — wraps the OpenAI SDK."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

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


def _get_openai_client(api_base: str | None = None):
    """Create an OpenAI client."""
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for the OpenAI provider. "
            "Install it with: pip install openai"
        ) from exc

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMAuthError(
            "No OpenAI credentials found. Set OPENAI_API_KEY in your environment.",
            status_code=401,
        )

    kwargs: dict = {"api_key": api_key}
    if api_base:
        kwargs["base_url"] = api_base

    return openai.OpenAI(**kwargs)


def _convert_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-format tool definitions to OpenAI function-calling format.

    Anthropic format:
        {"name": "weather", "description": "...", "input_schema": {...}}

    OpenAI format:
        {"type": "function", "function": {"name": "weather", "description": "...",
         "parameters": {...}}}
    """
    openai_tools = []
    for tool in tools:
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )
    return openai_tools


def _convert_messages_to_openai(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-format messages to OpenAI chat format.

    Handles:
    - Simple string content -> pass through
    - tool_use blocks -> OpenAI tool_calls format
    - tool_result blocks -> OpenAI tool role messages
    - text blocks in lists -> concatenated text
    """
    openai_messages = []

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        if isinstance(content, str):
            openai_messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            openai_messages.append({"role": role, "content": str(content)})
            continue

        # List content — could be text blocks, tool_use blocks, or tool_result blocks
        if role == "assistant":
            # Collect text parts and tool_calls
            text_parts = []
            tool_calls = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        }
                    )

            msg_dict: dict = {"role": "assistant"}
            if text_parts:
                msg_dict["content"] = "\n".join(text_parts)
            else:
                msg_dict["content"] = None
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
            openai_messages.append(msg_dict)

        elif role == "user":
            # Could be text blocks or tool_result blocks
            text_parts = []
            tool_results = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tool_results.append(block)
                elif block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

            # Emit tool results as separate "tool" role messages
            for tr in tool_results:
                result_content = tr.get("content", "")
                if isinstance(result_content, list):
                    # Flatten list content
                    parts = []
                    for item in result_content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            parts.append(item.get("text", ""))
                        else:
                            parts.append(str(item))
                    result_content = "\n".join(parts)
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": str(result_content),
                    }
                )

            # Emit remaining text as a user message
            if text_parts:
                openai_messages.append({"role": "user", "content": "\n".join(text_parts)})

        else:
            # system or other roles — concatenate text blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            openai_messages.append({"role": role, "content": "\n".join(text_parts)})

    return openai_messages


def _convert_response(response) -> LLMMessage:
    """Convert an OpenAI ChatCompletion response to LLMMessage."""
    choice = response.choices[0]
    msg = choice.message

    content: list[ContentBlock] = []

    # Text content
    if msg.content:
        content.append(TextBlock(text=msg.content))

    # Tool calls
    if msg.tool_calls:
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append(
                ToolUseBlock(
                    id=tc.id,
                    name=tc.function.name,
                    input=args,
                )
            )

    # Map finish_reason to our stop_reason
    finish_reason = choice.finish_reason or "stop"
    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    usage = None
    if hasattr(response, "usage") and response.usage:
        usage = Usage(
            input_tokens=getattr(response.usage, "prompt_tokens", 0),
            output_tokens=getattr(response.usage, "completion_tokens", 0),
        )

    return LLMMessage(content=content, stop_reason=stop_reason, usage=usage)


def _wrap_openai_error(exc: Exception) -> LLMProviderError:
    """Convert an OpenAI SDK error to a common error type."""
    import openai

    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitError(str(exc), status_code=429)
    if isinstance(exc, openai.AuthenticationError):
        return LLMAuthError(str(exc), status_code=401)
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if code in {500, 502, 503}:
            return LLMTransientError(str(exc), status_code=code)
        return LLMProviderError(str(exc), status_code=code)
    return LLMProviderError(str(exc))


class OpenAIProvider(LLMProvider):
    """LLM provider backed by the OpenAI Chat Completions API."""

    def __init__(self, api_base: str | None = None) -> None:
        self._api_base = api_base
        self._client = None

    def _get_client(self):
        """Return a cached OpenAI client, creating one on first use."""
        if self._client is None:
            self._client = _get_openai_client(self._api_base)
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
        import openai

        client = self._get_client()

        openai_messages = self.format_messages(messages)
        if system:
            openai_messages.insert(0, {"role": "system", "content": system})

        create_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": openai_messages,
        }
        if tools:
            create_kwargs["tools"] = self.format_tools(tools)
        if timeout is not None:
            create_kwargs["timeout"] = timeout

        try:
            response = client.chat.completions.create(**create_kwargs)
        except (openai.APIStatusError, openai.APIConnectionError) as exc:
            raise _wrap_openai_error(exc) from exc

        return _convert_response(response)

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
        import openai

        client = self._get_client()

        openai_messages = self.format_messages(messages)
        if system:
            openai_messages.insert(0, {"role": "system", "content": system})

        create_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": openai_messages,
            "stream": True,
        }
        if tools:
            create_kwargs["tools"] = self.format_tools(tools)

        try:
            stream = client.chat.completions.create(**create_kwargs)
        except (openai.APIStatusError, openai.APIConnectionError) as exc:
            raise _wrap_openai_error(exc) from exc

        # Accumulate the full response from stream chunks
        content_text = ""
        tool_calls_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
        finish_reason = "stop"
        prompt_tokens = 0
        completion_tokens = 0

        try:
            for chunk in stream:
                if not chunk.choices:
                    # Usage-only chunk at the end
                    if hasattr(chunk, "usage") and chunk.usage:
                        prompt_tokens = getattr(chunk.usage, "prompt_tokens", 0)
                        completion_tokens = getattr(chunk.usage, "completion_tokens", 0)
                    continue

                delta = chunk.choices[0].delta
                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

                # Text delta
                if delta.content:
                    content_text += delta.content
                    if on_text_delta is not None:
                        on_text_delta(delta.content)

                # Tool call deltas
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments": "",
                            }
                        if tc_delta.id:
                            tool_calls_acc[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_calls_acc[idx]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments
        except (openai.APIStatusError, openai.APIConnectionError) as exc:
            raise _wrap_openai_error(exc) from exc

        # Build final LLMMessage
        content: list[ContentBlock] = []
        if content_text:
            content.append(TextBlock(text=content_text))

        for _idx, tc in sorted(tool_calls_acc.items()):
            try:
                args = json.loads(tc["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append(ToolUseBlock(id=tc["id"], name=tc["name"], input=args))

        stop_reason_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }

        usage = Usage(input_tokens=prompt_tokens, output_tokens=completion_tokens)

        return LLMMessage(
            content=content,
            stop_reason=stop_reason_map.get(finish_reason, "end_turn"),
            usage=usage,
        )

    def format_tools(self, tool_defs: list[dict]) -> list[dict]:
        return _convert_tools_to_openai(tool_defs)

    def format_messages(self, messages: list[dict]) -> list[dict]:
        return _convert_messages_to_openai(messages)

    def health(self) -> bool:
        """Check connectivity by listing models."""
        try:
            client = self._get_client()
            next(iter(client.models.list()), None)
            return True
        except Exception:
            return False

    def extract_env_vars(self) -> dict[str, str]:
        env: dict[str, str] = {}
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        return env
