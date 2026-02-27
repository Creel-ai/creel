"""Mock LLM server implementing the Anthropic Messages API.

A lightweight FastAPI server for integration testing Creel. Supports echo mode,
scripted responses via regex triggers, tool call responses, streaming (SSE),
and error injection.

Usage:
    python scripts/test-harness/mock_llm_server.py [--port 18999]
    # Or with uvicorn:
    uvicorn scripts.test-harness.mock_llm_server:app --port 18999
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_triggers()
    yield


app = FastAPI(title="Mock Anthropic LLM Server", lifespan=lifespan)

# --- State ---

_call_history: list[dict] = []
_history_lock = Lock()

# Error injection: {"remaining": int, "status_code": int}
_error_state: dict[str, Any] = {"remaining": 0, "status_code": 500}
_error_lock = Lock()

# Scripted triggers loaded from fixtures/llm_triggers.json
_triggers: list[dict] = []

# Pending follow-ups keyed by conversation (for tool call → followup flows)
# Key: frozenset of (tool_use_id,) → followup text
_pending_followups: dict[str, str] = {}
_followup_lock = Lock()

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_triggers(path: Path | None = None) -> None:
    """Load scripted triggers from a JSON file."""
    global _triggers
    if path is None:
        path = FIXTURES_DIR / "llm_triggers.json"
    if path.exists():
        data = json.loads(path.read_text())
        _triggers = data.get("triggers", [])
        logger.info("Loaded %d triggers from %s", len(_triggers), path)
    else:
        _triggers = []
        logger.warning("No triggers file found at %s, running in echo-only mode", path)


def _make_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _make_tool_use_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:24]}"


def _extract_last_user_text(messages: list[dict]) -> str:
    """Extract the text content from the last user message."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
    return ""


def _has_tool_result(messages: list[dict]) -> tuple[bool, str | None, str | None]:
    """Check if the last user message contains a tool_result block.

    Returns (has_result, tool_use_id, result_content).
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        # Extract text from content blocks
                        parts = []
                        for sub in result_content:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                parts.append(sub.get("text", ""))
                        result_content = " ".join(parts)
                    return True, tool_use_id, str(result_content)
        break
    return False, None, None


def _match_trigger(text: str) -> dict | None:
    """Find the first trigger whose regex matches the user text."""
    for trigger in _triggers:
        pattern = trigger.get("match", "")
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return trigger
        except re.error:
            logger.warning("Invalid regex in trigger: %s", pattern)
    return None


def _build_text_response(
    text: str,
    model: str = "mock-claude",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> dict:
    """Build an Anthropic Messages API response with a text block."""
    return {
        "id": _make_msg_id(),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _build_tool_call_response(
    tool_name: str,
    tool_args: dict,
    model: str = "mock-claude",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> dict:
    """Build an Anthropic Messages API response with a tool_use block."""
    return {
        "id": _make_msg_id(),
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": _make_tool_use_id(),
                "name": tool_name,
                "input": tool_args,
            }
        ],
        "model": model,
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _generate_response(body: dict) -> dict:
    """Determine the appropriate response for a messages request."""
    messages = body.get("messages", [])
    model = body.get("model", "mock-claude")

    # Check if this is a tool result follow-up
    has_result, tool_use_id, result_content = _has_tool_result(messages)
    if has_result and tool_use_id:
        with _followup_lock:
            followup_template = _pending_followups.pop(tool_use_id, None)
        if followup_template:
            text = followup_template.replace("{tool_result}", result_content or "")
            return _build_text_response(text, model=model)
        # No specific followup configured; echo the tool result
        return _build_text_response(
            f"Tool result: {result_content}", model=model
        )

    # Extract user text for trigger matching
    user_text = _extract_last_user_text(messages)

    # Try scripted triggers (skip for empty input — fall through to echo)
    trigger = _match_trigger(user_text) if user_text else None
    if trigger:
        response_spec = trigger.get("response", {})
        resp_type = response_spec.get("type", "text")

        if resp_type == "text":
            content = response_spec.get("content", "")
            content = content.replace("{user_message}", user_text)
            return _build_text_response(content, model=model)

        elif resp_type == "tool_call":
            tool_name = response_spec.get("tool", "exec")
            tool_args = response_spec.get("args", {})
            followup = trigger.get("followup", "Done.")
            resp = _build_tool_call_response(tool_name, tool_args, model=model)
            # Store followup for when tool result comes back
            tool_use_id = resp["content"][0]["id"]
            with _followup_lock:
                _pending_followups[tool_use_id] = followup
            return resp

    # Default: echo mode
    echo_text = f"Echo: {user_text}" if user_text else "Echo: (empty message)"
    return _build_text_response(echo_text, model=model)


def _stream_response(body: dict):
    """Generator that yields SSE events in Anthropic streaming format."""
    response = _generate_response(body)
    msg_id = response["id"]
    model = response["model"]
    usage = response["usage"]

    # event: message_start
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': usage['input_tokens'], 'output_tokens': 0}}})}\n\n"

    for idx, block in enumerate(response["content"]):
        if block["type"] == "text":
            # content_block_start
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

            # Stream text in chunks
            text = block["text"]
            chunk_size = 20
            for i in range(0, len(text), chunk_size):
                chunk = text[i : i + chunk_size]
                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'text_delta', 'text': chunk}})}\n\n"

            # content_block_stop
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx})}\n\n"

        elif block["type"] == "tool_use":
            # content_block_start for tool_use
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': idx, 'content_block': {'type': 'tool_use', 'id': block['id'], 'name': block['name'], 'input': {}}})}\n\n"

            # Send input as a single JSON delta
            input_json = json.dumps(block["input"])
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'input_json_delta', 'partial_json': input_json}})}\n\n"

            # content_block_stop
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx})}\n\n"

    # message_delta with stop_reason and output token count
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': response['stop_reason'], 'stop_sequence': None}, 'usage': {'output_tokens': usage['output_tokens']}})}\n\n"

    # message_stop
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


# --- Endpoints ---


@app.post("/v1/messages")
async def create_message(request: Request):
    """Handle POST /v1/messages — the Anthropic Messages API endpoint."""
    body = await request.json()

    # Record in history
    with _history_lock:
        _call_history.append(
            {
                "timestamp": time.time(),
                "body": body,
                "headers": dict(request.headers),
            }
        )

    # Check error injection
    with _error_lock:
        if _error_state["remaining"] > 0:
            _error_state["remaining"] -= 1
            status_code = _error_state["status_code"]
            return JSONResponse(
                status_code=status_code,
                content={
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"Mock error injection (status {status_code})",
                    },
                },
            )

    # Check if streaming requested
    if body.get("stream", False):
        return StreamingResponse(
            _stream_response(body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    # Non-streaming response
    response = _generate_response(body)
    return JSONResponse(content=response)


@app.post("/v1/mock/error")
async def set_error(request: Request):
    """Inject errors: next N requests return the given status code."""
    body = await request.json()
    count = body.get("count", 1)
    status_code = body.get("status_code", 500)

    with _error_lock:
        _error_state["remaining"] = count
        _error_state["status_code"] = status_code

    return JSONResponse(
        content={"status": "ok", "error_count": count, "status_code": status_code}
    )


@app.post("/v1/mock/reset")
async def reset():
    """Reset all mock state: error injection, call history, pending followups."""
    with _error_lock:
        _error_state["remaining"] = 0
        _error_state["status_code"] = 500

    with _history_lock:
        _call_history.clear()

    with _followup_lock:
        _pending_followups.clear()

    return JSONResponse(content={"status": "ok"})


@app.get("/v1/mock/history")
async def get_history():
    """Return all recorded request history for test assertions."""
    with _history_lock:
        return JSONResponse(content={"calls": list(_call_history)})


@app.get("/health")
async def health():
    """Health check endpoint."""
    return JSONResponse(content={"status": "ok", "triggers": len(_triggers)})


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Mock Anthropic LLM Server")
    parser.add_argument("--port", type=int, default=18999, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logger.info("Starting mock LLM server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
