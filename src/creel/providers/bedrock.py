"""AWS Bedrock provider — wraps boto3 for Anthropic/Titan models on Bedrock."""

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


def _get_bedrock_client(region: str | None = None):
    """Create a Bedrock Runtime client."""
    try:
        import boto3
    except ImportError as exc:
        raise ImportError(
            "The 'boto3' package is required for the Bedrock provider. "
            "Install it with: pip install boto3"
        ) from exc

    kwargs = {}
    if region:
        kwargs["region_name"] = region

    return boto3.client("bedrock-runtime", **kwargs)


def _convert_bedrock_response(response_body: dict) -> LLMMessage:
    """Convert a Bedrock Anthropic response to LLMMessage.

    Bedrock wraps Anthropic models and returns the same message format
    as the Anthropic API when using the Messages API via `invoke_model`.
    """
    content: list[ContentBlock] = []
    for block in response_body.get("content", []):
        if block.get("type") == "text":
            content.append(TextBlock(text=block.get("text", "")))
        elif block.get("type") == "tool_use":
            content.append(
                ToolUseBlock(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    input=block.get("input", {}),
                )
            )

    usage = None
    usage_data = response_body.get("usage")
    if usage_data:
        usage = Usage(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
        )

    return LLMMessage(
        content=content,
        stop_reason=response_body.get("stop_reason", "end_turn"),
        usage=usage,
    )


def _wrap_bedrock_error(exc: Exception) -> LLMProviderError:
    """Convert a boto3/botocore error to a common error type."""
    error_str = str(exc)
    # Try to extract HTTP status from ClientError
    status = None
    try:
        status = exc.response["ResponseMetadata"]["HTTPStatusCode"]  # type: ignore[attr-defined]
    except (AttributeError, KeyError, TypeError):
        pass

    if status == 429 or "ThrottlingException" in error_str:
        return LLMRateLimitError(error_str, status_code=429)
    if status in {401, 403} or "AccessDeniedException" in error_str:
        return LLMAuthError(error_str, status_code=status or 403)
    if status and status >= 500:
        return LLMTransientError(error_str, status_code=status)
    return LLMProviderError(error_str, status_code=status)


class BedrockProvider(LLMProvider):
    """LLM provider backed by AWS Bedrock Runtime.

    Supports Anthropic models on Bedrock using the Messages API format.
    Authentication uses standard AWS credential chain (env vars, IAM role,
    profiles, etc.).
    """

    def __init__(self, region: str | None = None) -> None:
        self._region = region

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
        client = _get_bedrock_client(self._region)

        # Build Anthropic Messages API payload for Bedrock
        body: dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools

        try:
            response = client.invoke_model(
                modelId=model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
        except Exception as exc:
            raise _wrap_bedrock_error(exc) from exc

        response_body = json.loads(response["body"].read())
        return _convert_bedrock_response(response_body)

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
        client = _get_bedrock_client(self._region)

        body: dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools

        try:
            response = client.invoke_model_with_response_stream(
                modelId=model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
        except Exception as exc:
            raise _wrap_bedrock_error(exc) from exc

        # Accumulate streamed chunks
        content: list[ContentBlock] = []
        current_text = ""
        current_tool: dict | None = None
        stop_reason = "end_turn"
        usage_input = 0
        usage_output = 0

        try:
            event_stream = response["body"]
            for event in event_stream:
                chunk = event.get("chunk")
                if not chunk:
                    continue

                chunk_data = json.loads(chunk["bytes"])
                chunk_type = chunk_data.get("type")

                if chunk_type == "content_block_start":
                    block = chunk_data.get("content_block", {})
                    if block.get("type") == "text":
                        current_text = block.get("text", "")
                    elif block.get("type") == "tool_use":
                        current_tool = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input_json": "",
                        }

                elif chunk_type == "content_block_delta":
                    delta = chunk_data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        current_text += text
                        if on_text_delta and text:
                            on_text_delta(text)
                    elif delta.get("type") == "input_json_delta":
                        if current_tool is not None:
                            current_tool["input_json"] += delta.get("partial_json", "")

                elif chunk_type == "content_block_stop":
                    if current_tool is not None:
                        try:
                            args = json.loads(current_tool["input_json"])
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        content.append(
                            ToolUseBlock(
                                id=current_tool["id"],
                                name=current_tool["name"],
                                input=args,
                            )
                        )
                        current_tool = None
                    elif current_text:
                        content.append(TextBlock(text=current_text))
                        current_text = ""

                elif chunk_type == "message_delta":
                    delta = chunk_data.get("delta", {})
                    if delta.get("stop_reason"):
                        stop_reason = delta["stop_reason"]
                    usage_delta = chunk_data.get("usage", {})
                    usage_output += usage_delta.get("output_tokens", 0)

                elif chunk_type == "message_start":
                    msg = chunk_data.get("message", {})
                    msg_usage = msg.get("usage", {})
                    usage_input = msg_usage.get("input_tokens", 0)

        except Exception as exc:
            raise _wrap_bedrock_error(exc) from exc

        # Flush any remaining text
        if current_text:
            content.append(TextBlock(text=current_text))

        return LLMMessage(
            content=content,
            stop_reason=stop_reason,
            usage=Usage(input_tokens=usage_input, output_tokens=usage_output),
        )

    def extract_env_vars(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION",
        ):
            val = os.environ.get(key)
            if val:
                env[key] = val
        if self._region:
            env["AWS_REGION"] = self._region
        return env
