"""Ollama provider — uses Ollama's OpenAI-compatible API via httpx."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx

from creel.providers.base import (
    ContentBlock,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMTransientError,
    TextBlock,
    ToolUseBlock,
    Usage,
)

logger = logging.getLogger(__name__)

_DEFAULT_OLLAMA_BASE = "http://localhost:11434"


class OllamaProvider(LLMProvider):
    """LLM provider backed by Ollama's OpenAI-compatible chat API.

    Ollama exposes an OpenAI-compatible endpoint at /v1/chat/completions.
    Tool calling support depends on the model — gracefully degrades when
    the model doesn't support it.
    """

    def __init__(self, api_base: str | None = None) -> None:
        self._api_base = (api_base or _DEFAULT_OLLAMA_BASE).rstrip("/")

    def _chat_url(self) -> str:
        return f"{self._api_base}/v1/chat/completions"

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert Anthropic tool defs to OpenAI function-calling format."""
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

    def _convert_messages(self, messages: list[dict], system: str | None = None) -> list[dict]:
        """Convert Anthropic messages to OpenAI chat format for Ollama."""
        # Re-use the OpenAI provider's conversion logic
        from creel.providers.openai import _convert_messages_to_openai

        result = _convert_messages_to_openai(messages)
        if system:
            result.insert(0, {"role": "system", "content": system})
        return result

    def _parse_response(self, data: dict) -> LLMMessage:
        """Parse an OpenAI-compatible chat completion response."""
        choice = data["choices"][0]
        msg = choice["message"]

        content: list[ContentBlock] = []
        if msg.get("content"):
            content.append(TextBlock(text=msg["content"]))

        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            func = tc.get("function", {})
            try:
                args = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append(
                ToolUseBlock(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    input=args,
                )
            )

        finish_reason = choice.get("finish_reason", "stop")
        stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}

        usage = None
        usage_data = data.get("usage")
        if usage_data:
            usage = Usage(
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
            )

        return LLMMessage(
            content=content,
            stop_reason=stop_map.get(finish_reason, "end_turn"),
            usage=usage,
        )

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
        ollama_messages = self._convert_messages(messages, system)

        payload: dict = {
            "model": model,
            "messages": ollama_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)

        try:
            resp = httpx.post(
                self._chat_url(),
                json=payload,
                timeout=timeout or 120.0,
            )
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise LLMProviderError(
                f"Cannot connect to Ollama at {self._api_base}. Is Ollama running? Error: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code >= 500:
                raise LLMTransientError(str(exc), status_code=code) from exc
            raise LLMProviderError(str(exc), status_code=code) from exc

        return self._parse_response(resp.json())

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
        ollama_messages = self._convert_messages(messages, system)

        payload: dict = {
            "model": model,
            "messages": ollama_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)

        try:
            with httpx.stream(
                "POST",
                self._chat_url(),
                json=payload,
                timeout=120.0,
            ) as resp:
                resp.raise_for_status()

                content_text = ""
                tool_calls_acc: dict[int, dict] = {}
                finish_reason = "stop"
                prompt_tokens = 0
                completion_tokens = 0

                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if not chunk.get("choices"):
                        usage_data = chunk.get("usage")
                        if usage_data:
                            prompt_tokens = usage_data.get("prompt_tokens", 0)
                            completion_tokens = usage_data.get("completion_tokens", 0)
                        continue

                    delta = chunk["choices"][0].get("delta", {})
                    if chunk["choices"][0].get("finish_reason"):
                        finish_reason = chunk["choices"][0]["finish_reason"]

                    if delta.get("content"):
                        content_text += delta["content"]
                        if on_text_delta:
                            on_text_delta(delta["content"])

                    if delta.get("tool_calls"):
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": tc_delta.get("id", ""),
                                    "name": "",
                                    "arguments": "",
                                }
                            if tc_delta.get("id"):
                                tool_calls_acc[idx]["id"] = tc_delta["id"]
                            func = tc_delta.get("function", {})
                            if func.get("name"):
                                tool_calls_acc[idx]["name"] = func["name"]
                            if func.get("arguments"):
                                tool_calls_acc[idx]["arguments"] += func["arguments"]

        except httpx.ConnectError as exc:
            raise LLMProviderError(
                f"Cannot connect to Ollama at {self._api_base}. Is Ollama running? Error: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code >= 500:
                raise LLMTransientError(str(exc), status_code=code) from exc
            raise LLMProviderError(str(exc), status_code=code) from exc

        # Build final message
        content: list[ContentBlock] = []
        if content_text:
            content.append(TextBlock(text=content_text))
        for _idx, tc in sorted(tool_calls_acc.items()):
            try:
                args = json.loads(tc["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append(ToolUseBlock(id=tc["id"], name=tc["name"], input=args))

        stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}

        return LLMMessage(
            content=content,
            stop_reason=stop_map.get(finish_reason, "end_turn"),
            usage=Usage(input_tokens=prompt_tokens, output_tokens=completion_tokens),
        )

    def format_tools(self, tool_defs: list[dict]) -> list[dict]:
        return self._convert_tools(tool_defs)

    def format_messages(self, messages: list[dict]) -> list[dict]:
        from creel.providers.openai import _convert_messages_to_openai

        return _convert_messages_to_openai(messages)

    def extract_env_vars(self) -> dict[str, str]:
        return {"OLLAMA_HOST": self._api_base}
