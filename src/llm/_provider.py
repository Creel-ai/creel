"""Thin LLM provider abstraction for containerized runners.

Self-contained (no creel.* imports) so it works inside the minimalist
LLM runner Docker container.  Reads the ``PROVIDER`` environment variable
to determine which SDK to use (default: ``anthropic``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

# OAuth tokens (sk-ant-oat) authenticate via Claude Code's subscription path.
_OAUTH_HEADERS = {
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
    "user-agent": "claude-cli/2.1.59 (external, cli)",
    "x-app": "cli",
}

_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def _is_oauth_token(token: str) -> bool:
    return "sk-ant-oat" in token


@dataclass
class ContainerLLMResponse:
    """Simplified LLM response for container runners.

    ``content`` is a list of dicts in Anthropic format:
      - ``{"type": "text", "text": "..."}``
      - ``{"type": "tool_use", "id": "...", "name": "...", "input": {...}}``
    """

    content: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


class ContainerProvider:
    """Base interface for container LLM providers."""

    def create(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> ContainerLLMResponse:
        raise NotImplementedError


class AnthropicContainerProvider(ContainerProvider):
    """Anthropic provider using the ``anthropic`` SDK."""

    def __init__(self) -> None:
        import anthropic

        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        self._is_oauth = bool(auth_token and _is_oauth_token(auth_token))

        if auth_token:
            headers = _OAUTH_HEADERS if self._is_oauth else {}
            self._client = anthropic.Anthropic(auth_token=auth_token, default_headers=headers)
        elif api_key:
            self._client = anthropic.Anthropic(api_key=api_key)
        else:
            raise RuntimeError("Set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY")

    def create(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> ContainerLLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        resolved_system, messages = self._resolve_for_oauth(system, messages)
        kwargs["messages"] = messages
        if resolved_system:
            kwargs["system"] = resolved_system
        if tools:
            kwargs["tools"] = tools

        resp = self._client.messages.create(**kwargs)

        content: list[dict] = []
        for block in resp.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        usage = resp.usage
        return ContainerLLMResponse(
            content=content,
            stop_reason=resp.stop_reason or "end_turn",
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        )

    def _resolve_for_oauth(
        self,
        system: str | None,
        messages: list[dict],
    ) -> tuple[str | None, list[dict]]:
        if not self._is_oauth:
            return system, messages
        if not system:
            return _CLAUDE_CODE_SYSTEM_PREFIX, messages
        preamble = {
            "role": "user",
            "content": (
                f"[IMPORTANT INSTRUCTIONS — follow these for the entire conversation]\n\n{system}"
            ),
        }
        ack = {"role": "assistant", "content": "Understood. I'll follow those instructions."}
        return _CLAUDE_CODE_SYSTEM_PREFIX, [preamble, ack, *messages]


class OpenAIContainerProvider(ContainerProvider):
    """OpenAI provider using the ``openai`` SDK."""

    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "The 'openai' package is required for the OpenAI provider. "
                "Install it with: pip install openai"
            ) from exc

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Set OPENAI_API_KEY")

        kwargs: dict[str, Any] = {"api_key": api_key}
        api_base = os.environ.get("OPENAI_API_BASE")
        if api_base:
            kwargs["base_url"] = api_base

        self._client = OpenAI(**kwargs)

    def create(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> ContainerLLMResponse:
        oai_messages: list[dict] = []
        if system:
            oai_messages.append({"role": "system", "content": system})

        for msg in messages:
            oai_messages.extend(_convert_msg_to_openai(msg))

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": oai_messages,
        }
        if tools:
            kwargs["tools"] = _convert_tools_to_openai(tools)

        resp = self._client.chat.completions.create(**kwargs)
        return _parse_openai_response(resp)


class BedrockContainerProvider(ContainerProvider):
    """AWS Bedrock provider using ``boto3``."""

    def __init__(self) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise ImportError(
                "The 'boto3' package is required for the Bedrock provider. "
                "Install it with: pip install boto3"
            ) from exc

        region = os.environ.get("AWS_REGION", "us-east-1")
        self._client = boto3.client("bedrock-runtime", region_name=region)

    def create(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> ContainerLLMResponse:
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools

        resp = self._client.invoke_model(
            modelId=model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
        result = json.loads(resp["body"].read())

        content: list[dict] = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                content.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    }
                )

        usage = result.get("usage", {})
        return ContainerLLMResponse(
            content=content,
            stop_reason=result.get("stop_reason", "end_turn"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )


class OllamaContainerProvider(ContainerProvider):
    """Ollama provider using its OpenAI-compatible endpoint via ``httpx``."""

    def __init__(self) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "The 'httpx' package is required for the Ollama provider. "
                "Install it with: pip install httpx"
            ) from exc

        self._api_base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

    def create(
        self,
        *,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> ContainerLLMResponse:
        import httpx

        oai_messages: list[dict] = []
        if system:
            oai_messages.append({"role": "system", "content": system})

        for msg in messages:
            oai_messages.extend(_convert_msg_to_openai(msg))

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": oai_messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = _convert_tools_to_openai(tools)

        resp = httpx.post(
            f"{self._api_base}/v1/chat/completions",
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        return _parse_openai_dict_response(data)


# -- Provider factory --


def get_container_provider() -> ContainerProvider:
    """Create a provider based on the ``PROVIDER`` environment variable.

    Returns:
        A ``ContainerProvider`` instance ready for ``create()`` calls.

    Raises:
        ValueError: If the provider name is unknown.
        RuntimeError: If required credentials are missing.
        ImportError: If the provider's SDK is not installed.
    """
    provider_name = os.environ.get("PROVIDER", "anthropic").lower()

    if provider_name == "anthropic":
        return AnthropicContainerProvider()
    if provider_name == "openai":
        return OpenAIContainerProvider()
    if provider_name == "bedrock":
        return BedrockContainerProvider()
    if provider_name == "ollama":
        return OllamaContainerProvider()

    raise ValueError(
        f"Unknown provider: {provider_name!r}. Supported: anthropic, openai, bedrock, ollama"
    )


# -- OpenAI format helpers (shared by OpenAI and Ollama providers) --


def _convert_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-format tool defs to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def _convert_msg_to_openai(msg: dict) -> list[dict]:
    """Convert a single Anthropic-format message to OpenAI message(s).

    Tool results may expand to multiple messages.
    """
    content = msg.get("content")
    role = msg["role"]

    # Simple string content
    if isinstance(content, str) or content is None:
        return [msg]

    # List of content blocks
    if not isinstance(content, list):
        return [msg]

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []

    for block in content:
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )
        elif block_type == "tool_result":
            tool_results.append(block)

    result: list[dict] = []

    # Tool results → OpenAI "tool" role messages
    for tr in tool_results:
        result.append(
            {
                "role": "tool",
                "tool_call_id": tr["tool_use_id"],
                "content": tr.get("content", ""),
            }
        )

    # Assistant message with optional tool_calls
    if role == "assistant":
        oai_msg: dict[str, Any] = {"role": "assistant"}
        oai_msg["content"] = "\n".join(text_parts) if text_parts else None
        if tool_calls:
            oai_msg["tool_calls"] = tool_calls
        result.append(oai_msg)
    elif text_parts and not tool_results:
        # Plain user/system text
        result.append({"role": role, "content": "\n".join(text_parts)})
    elif text_parts:
        # Text alongside tool results
        result.append({"role": "user", "content": "\n".join(text_parts)})

    return result if result else [msg]


def _parse_openai_response(resp: Any) -> ContainerLLMResponse:
    """Parse an OpenAI SDK response object into ContainerLLMResponse."""
    choice = resp.choices[0]
    content: list[dict] = []

    if choice.message.content:
        content.append({"type": "text", "text": choice.message.content})

    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": args,
                }
            )

    # Map OpenAI finish reasons to Anthropic-style
    stop_reason = "end_turn"
    if choice.finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif choice.finish_reason == "length":
        stop_reason = "max_tokens"

    usage = resp.usage
    return ContainerLLMResponse(
        content=content,
        stop_reason=stop_reason,
        input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
    )


def _parse_openai_dict_response(data: dict) -> ContainerLLMResponse:
    """Parse an OpenAI-compatible JSON dict response (e.g. from Ollama)."""
    choice = data["choices"][0]
    message = choice["message"]
    content: list[dict] = []

    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})

    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            args = tc["function"]["arguments"]
            content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(args) if isinstance(args, str) else args,
                }
            )

    stop_reason = "end_turn"
    if choice.get("finish_reason") == "tool_calls":
        stop_reason = "tool_use"
    elif choice.get("finish_reason") == "length":
        stop_reason = "max_tokens"

    usage = data.get("usage", {})
    return ContainerLLMResponse(
        content=content,
        stop_reason=stop_reason,
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
    )
