"""Chat UI endpoints: serves the web chat interface and handles WebSocket streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response

from creel.daemon.contracts import StreamEvent

logger = logging.getLogger(__name__)

_CHAT_STATIC_DIR = Path(__file__).resolve().parent.parent / "chat_static"

router = APIRouter(tags=["chat"])
ws_router = APIRouter(tags=["chat"])


@router.get("/chat")
async def chat_ui() -> Response:
    """Serve the chat UI HTML page."""
    index = _CHAT_STATIC_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse("<h1>Chat UI not found</h1>", status_code=404)
    content = index.read_text(encoding="utf-8")
    return HTMLResponse(content)


@router.get("/chat/sessions")
async def chat_list_sessions(
    request: Request,
    sender_id: str = Query("web-chat", min_length=1),
) -> list[dict]:
    """List sessions for the chat UI."""
    svc = request.app.state.service
    rows = await asyncio.to_thread(svc.list_sessions, sender_id)
    return [{"sender_id": sender_id, **row} for row in rows]


@router.post("/chat/send")
async def chat_send(request: Request) -> dict:
    """Send a message (non-streaming) via chat UI."""
    body = await request.json()
    sender_id = body.get("sender_id", "web-chat")
    text = body.get("text", "")
    session_id = body.get("session_id")

    if not text.strip():
        return {"error": "empty message"}

    svc = request.app.state.service

    if session_id:
        await asyncio.to_thread(svc.resume_session, sender_id, session_id)

    response_text = await asyncio.to_thread(svc.send_message, sender_id, text, auto_approve=False)
    active_session_id = await asyncio.to_thread(svc.get_active_session_id, sender_id)

    return {
        "sender_id": sender_id,
        "text": response_text,
        "session_id": active_session_id,
    }


@ws_router.websocket("/chat/ws")
async def chat_ws(websocket: WebSocket) -> None:
    """WebSocket endpoint for streaming chat messages.

    Client sends JSON messages:
        {"type": "message", "text": "...", "sender_id": "...", "session_id": "..."}
        {"type": "new_session", "sender_id": "..."}
        {"type": "resume_session", "sender_id": "...", "session_id": "..."}
        {"type": "history", "sender_id": "...", "session_id": "...", "limit": 50}

    Server sends JSON events:
        {"type": "start", "session_id": "..."}
        {"type": "token", "text": "..."}
        {"type": "tool_call", "name": "...", "input": {...}}
        {"type": "tool_result", "name": "...", "output": "..."}
        {"type": "final", "text": "...", "session_id": "..."}
        {"type": "error", "error": "..."}
        {"type": "sessions", "sessions": [...]}
        {"type": "history", "messages": [...], "session_id": "..."}
        {"type": "session_created", "session_id": "...", ...}
    """
    # Optional token auth via query param
    expected_token = getattr(websocket.app.state, "dashboard_token", None)
    client_token = websocket.query_params.get("token")
    require_auth = websocket.query_params.get("auth") == "required"

    if require_auth and expected_token:
        if not client_token or client_token != expected_token:
            await websocket.close(code=4401, reason="unauthorized")
            return

    await websocket.accept()
    svc = websocket.app.state.service

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "error": "invalid JSON"})
                continue

            msg_type = msg.get("type", "message")
            sender_id = msg.get("sender_id", "web-chat")

            if msg_type == "message":
                await _handle_chat_message(websocket, svc, msg, sender_id)
            elif msg_type == "new_session":
                await _handle_new_session(websocket, svc, sender_id)
            elif msg_type == "resume_session":
                session_id = msg.get("session_id", "")
                await _handle_resume_session(websocket, svc, sender_id, session_id)
            elif msg_type == "history":
                session_id = msg.get("session_id")
                limit = msg.get("limit", 50)
                await _handle_history(websocket, svc, sender_id, session_id, limit)
            elif msg_type == "sessions":
                await _handle_list_sessions(websocket, svc, sender_id)
            else:
                await websocket.send_json(
                    {"type": "error", "error": f"unknown message type: {msg_type}"}
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


async def _handle_chat_message(
    websocket: WebSocket,
    svc: Any,
    msg: dict,
    sender_id: str,
) -> None:
    """Stream an agent response back over WebSocket."""
    text = msg.get("text", "")
    session_id = msg.get("session_id")

    if not text.strip():
        await websocket.send_json({"type": "error", "error": "empty message"})
        return

    q: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def _produce():
        try:
            for event in svc.stream_message(
                sender_id=sender_id,
                text=text,
                session_id=session_id,
                auto_approve=False,
            ):
                asyncio.run_coroutine_threadsafe(q.put(event), loop).result()
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(
                q.put(
                    {
                        "type": "error",
                        "sender_id": sender_id,
                        "payload": {"error": str(exc)},
                    }
                ),
                loop,
            ).result()
        finally:
            asyncio.run_coroutine_threadsafe(q.put(sentinel), loop).result()

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _produce)

    while True:
        item = await q.get()
        if item is sentinel:
            break
        evt = StreamEvent(**item)
        out: dict[str, Any] = {"type": evt.type}
        if evt.session_id:
            out["session_id"] = evt.session_id
        if evt.type == "token":
            out["text"] = evt.payload.get("text", "")
        elif evt.type == "final":
            out["text"] = evt.payload.get("text", "")
        elif evt.type == "error":
            out["error"] = evt.payload.get("error", "unknown error")
        elif evt.type == "tool_call":
            out["name"] = evt.payload.get("name", "")
            out["input"] = evt.payload.get("input", {})
        elif evt.type == "tool_result":
            out["name"] = evt.payload.get("name", "")
            out["output"] = evt.payload.get("output", "")
        try:
            await websocket.send_json(out)
        except WebSocketDisconnect:
            return


async def _handle_new_session(websocket: WebSocket, svc: Any, sender_id: str) -> None:
    """Create a new session and notify the client."""
    row = await asyncio.to_thread(svc.new_session, sender_id)
    await websocket.send_json({"type": "session_created", **row})


async def _handle_resume_session(
    websocket: WebSocket, svc: Any, sender_id: str, session_id: str
) -> None:
    """Resume a session and notify the client."""
    try:
        row = await asyncio.to_thread(svc.resume_session, sender_id, session_id)
        await websocket.send_json({"type": "session_resumed", **row})
    except ValueError as exc:
        await websocket.send_json({"type": "error", "error": str(exc)})


async def _handle_history(
    websocket: WebSocket,
    svc: Any,
    sender_id: str,
    session_id: str | None,
    limit: int,
) -> None:
    """Send message history to the client."""
    try:
        messages = await asyncio.to_thread(
            svc.get_history,
            sender_id=sender_id,
            session_id=session_id,
            limit=min(limit, 200),
        )
        await websocket.send_json(
            {
                "type": "history",
                "session_id": session_id,
                "messages": messages,
            }
        )
    except ValueError as exc:
        await websocket.send_json({"type": "error", "error": str(exc)})


async def _handle_list_sessions(websocket: WebSocket, svc: Any, sender_id: str) -> None:
    """Send session list to the client."""
    rows = await asyncio.to_thread(svc.list_sessions, sender_id)
    await websocket.send_json(
        {
            "type": "sessions",
            "sessions": [{"sender_id": sender_id, **row} for row in rows],
        }
    )
