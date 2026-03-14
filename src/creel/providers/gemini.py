"""Google Gemini provider — wraps the google-generativeai SDK."""

from __future__ import annotations

import itertools
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

# Thread-safe counter for generating unique tool-call IDs within a session
_tool_call_ids = itertools.count(1)


def _next_tool_call_id() -> str:
    """Generate a unique tool-call ID."""
    return f"gemini_tc_{next(_tool_call_ids)}"


def _get_genai_module():
    """Lazy-import google.generativeai."""
    try:
        import google.generativeai as genai
    except ImportError as exc:
        raise ImportError(
            "The 'google-generativeai' package is required for the Gemini provider. "
            "Install it with: pip install google-generativeai"
        ) from exc
    return genai


def _get_gemini_client():
    """Configure and return the google.generativeai module."""
    genai = _get_genai_module()

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMAuthError(
            "No Google AI credentials found. Set GOOGLE_API_KEY or GEMINI_API_KEY "
            "in your environment.",
            status_code=401,
        )

    genai.configure(api_key=api_key)
    return genai


def _convert_tools_to_gemini(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-format tool definitions to Gemini function declarations.

    Anthropic format:
        {"name": "weather", "description": "...", "input_schema": {...}}

    Gemini format (function declarations):
        {"name": "weather", "description": "...", "parameters": {...}}
    """
    declarations = []
    for tool in tools:
        schema = tool.get("input_schema", {})
        # Gemini doesn't accept the top-level "type": "object" the same way;
        # it expects a simplified schema structure
        params = {}
        if schema:
            params = {k: v for k, v in schema.items() if k != "additionalProperties"}
        declarations.append(
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": params if params else None,
            }
        )
    return declarations


def _convert_messages_to_gemini(
    messages: list[dict],
) -> list[dict]:
    """Convert Anthropic-format messages to Gemini content format.

    Gemini uses:
        {"role": "user"|"model", "parts": [{"text": "..."}, ...]}

    Tool calls become function_call parts, tool results become function_response parts.
    Gemini requires ``function_response.name`` to be the **function name** (not the
    call ID), so we build a lookup from ``tool_use_id`` → ``name`` while scanning.
    """
    gemini_contents = []

    # Build lookup: tool_use_id -> function name from tool_use blocks
    tool_id_to_name: dict[str, str] = {}
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id", "")
                    if tid:
                        tool_id_to_name[tid] = block.get("name", "unknown")

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        # Map Anthropic roles to Gemini roles
        gemini_role = "model" if role == "assistant" else "user"

        if isinstance(content, str):
            gemini_contents.append({"role": gemini_role, "parts": [{"text": content}]})
            continue

        if not isinstance(content, list):
            gemini_contents.append({"role": gemini_role, "parts": [{"text": str(content)}]})
            continue

        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")

            if block_type == "text":
                text = block.get("text", "")
                if text:
                    parts.append({"text": text})

            elif block_type == "tool_use":
                parts.append(
                    {
                        "function_call": {
                            "name": block.get("name", ""),
                            "args": block.get("input", {}),
                        }
                    }
                )

            elif block_type == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    text_parts = []
                    for item in result_content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        else:
                            text_parts.append(str(item))
                    result_content = "\n".join(text_parts)
                tool_use_id = block.get("tool_use_id", "")
                func_name = tool_id_to_name.get(tool_use_id, "unknown")
                parts.append(
                    {
                        "function_response": {
                            "name": func_name,
                            "response": {"result": str(result_content)},
                        }
                    }
                )

        if parts:
            gemini_contents.append({"role": gemini_role, "parts": parts})

    return gemini_contents


def _convert_gemini_response(response) -> LLMMessage:
    """Convert a Gemini GenerateContentResponse to LLMMessage."""
    content: list[ContentBlock] = []
    has_tool_calls = False

    for candidate in response.candidates:
        for part in candidate.content.parts:
            if hasattr(part, "text") and part.text:
                content.append(TextBlock(text=part.text))
            elif hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                # Convert MapComposite args to a plain dict
                args = dict(fc.args) if fc.args else {}
                content.append(
                    ToolUseBlock(
                        id=_next_tool_call_id(),
                        name=fc.name,
                        input=args,
                    )
                )
                has_tool_calls = True

    stop_reason = "tool_use" if has_tool_calls else "end_turn"

    # Check for max tokens
    finish_reason = getattr(response.candidates[0], "finish_reason", None)
    if finish_reason is not None:
        # Gemini uses enum values; 2 = MAX_TOKENS
        fr_value = (
            finish_reason
            if isinstance(finish_reason, int)
            else getattr(finish_reason, "value", None)
        )
        if fr_value == 2:
            stop_reason = "max_tokens"

    usage = None
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        um = response.usage_metadata
        usage = Usage(
            input_tokens=getattr(um, "prompt_token_count", 0) or 0,
            output_tokens=getattr(um, "candidates_token_count", 0) or 0,
        )

    return LLMMessage(content=content, stop_reason=stop_reason, usage=usage)


def _wrap_gemini_error(exc: Exception) -> LLMProviderError:
    """Convert a google.generativeai error to a common error type."""
    msg = str(exc)
    # The SDK raises google.api_core.exceptions for HTTP errors
    status_code = getattr(exc, "code", None)
    if status_code is None:
        # Try grpc_status_code
        status_code = getattr(exc, "grpc_status_code", None)

    if status_code == 429 or "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return LLMRateLimitError(msg, status_code=429)
    if status_code in {401, 403} or "PERMISSION_DENIED" in msg or "UNAUTHENTICATED" in msg:
        return LLMAuthError(msg, status_code=status_code or 403)
    if status_code in {500, 502, 503} or "UNAVAILABLE" in msg or "INTERNAL" in msg:
        return LLMTransientError(msg, status_code=status_code or 500)
    return LLMProviderError(msg, status_code=status_code)


class GeminiProvider(LLMProvider):
    """LLM provider backed by the Google Gemini API."""

    def __init__(self) -> None:
        self._configured = False

    def _ensure_configured(self):
        """Configure the SDK on first use."""
        if not self._configured:
            _get_gemini_client()
            self._configured = True

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
        self._ensure_configured()
        genai = _get_genai_module()

        generation_config = {"max_output_tokens": max_tokens}

        model_kwargs: dict = {"model_name": model, "generation_config": generation_config}
        if system:
            model_kwargs["system_instruction"] = system
        if tools:
            model_kwargs["tools"] = [{"function_declarations": _convert_tools_to_gemini(tools)}]

        gemini_model = genai.GenerativeModel(**model_kwargs)
        gemini_contents = _convert_messages_to_gemini(messages)

        try:
            response = gemini_model.generate_content(gemini_contents)
        except Exception as exc:
            raise _wrap_gemini_error(exc) from exc

        return _convert_gemini_response(response)

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
        self._ensure_configured()
        genai = _get_genai_module()

        generation_config = {"max_output_tokens": max_tokens}

        model_kwargs: dict = {"model_name": model, "generation_config": generation_config}
        if system:
            model_kwargs["system_instruction"] = system
        if tools:
            model_kwargs["tools"] = [{"function_declarations": _convert_tools_to_gemini(tools)}]

        gemini_model = genai.GenerativeModel(**model_kwargs)
        gemini_contents = _convert_messages_to_gemini(messages)

        content: list[ContentBlock] = []
        has_tool_calls = False
        all_text = ""
        usage_input = 0
        usage_output = 0

        try:
            response = gemini_model.generate_content(gemini_contents, stream=True)

            for chunk in response:
                if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                    um = chunk.usage_metadata
                    usage_input = getattr(um, "prompt_token_count", 0) or 0
                    usage_output = getattr(um, "candidates_token_count", 0) or 0

                if not chunk.candidates:
                    continue

                for part in chunk.candidates[0].content.parts:
                    if hasattr(part, "text") and part.text:
                        all_text += part.text
                        if on_text_delta:
                            on_text_delta(part.text)
                    elif hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        args = dict(fc.args) if fc.args else {}
                        content.append(
                            ToolUseBlock(
                                id=_next_tool_call_id(),
                                name=fc.name,
                                input=args,
                            )
                        )
                        has_tool_calls = True
        except Exception as exc:
            raise _wrap_gemini_error(exc) from exc

        if all_text:
            content.insert(0, TextBlock(text=all_text))

        stop_reason = "tool_use" if has_tool_calls else "end_turn"

        return LLMMessage(
            content=content,
            stop_reason=stop_reason,
            usage=Usage(input_tokens=usage_input, output_tokens=usage_output),
        )

    def format_tools(self, tool_defs: list[dict]) -> list[dict]:
        return _convert_tools_to_gemini(tool_defs)

    def format_messages(self, messages: list[dict]) -> list[dict]:
        return _convert_messages_to_gemini(messages)

    def health(self) -> bool:
        """Check connectivity by listing models."""
        try:
            genai = _get_gemini_client()
            # list_models returns an iterator; consume first item
            next(iter(genai.list_models()), None)
            return True
        except Exception:
            return False

    def extract_env_vars(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
            val = os.environ.get(key)
            if val:
                env[key] = val
        return env
