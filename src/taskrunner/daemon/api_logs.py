"""Log streaming API endpoints for the Creel dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["logs"])
# WebSocket routes need a separate router — they can't use HTTPBearer auth
# dependencies (HTTPBearer requires an HTTP Request object). The ws_logs
# handler authenticates via query parameter instead.
ws_router = APIRouter(tags=["logs"])

# Pattern for standard log lines: "2026-02-25 09:30:00 [INFO] taskrunner.daemon: message"
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s+([\w.]+):\s+(.*)$"
)

# Log level ordering for filtering
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "WARN": 2, "ERROR": 3, "CRITICAL": 4}


def _daemon_log_path() -> Path:
    """Return path to daemon.log."""
    creel_home = Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))
    return creel_home / "daemon.log"


def _parse_log_line(line: str) -> dict[str, str] | None:
    """Parse a log line into structured JSON. Returns None for unparseable lines."""
    line = line.rstrip("\n\r")
    if not line:
        return None

    m = _LOG_LINE_RE.match(line)
    if m:
        return {
            "timestamp": m.group(1),
            "level": m.group(2),
            "module": m.group(3),
            "message": m.group(4),
        }

    # Try JSON-formatted log lines
    try:
        data = json.loads(line)
        if isinstance(data, dict) and "level" in data:
            return {
                "timestamp": data.get("timestamp", ""),
                "level": data.get("level", "INFO"),
                "module": data.get("logger", data.get("module", "")),
                "message": data.get("message", ""),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    # Unparseable line — return as raw message
    return {
        "timestamp": "",
        "level": "INFO",
        "module": "",
        "message": line,
    }


def _passes_level_filter(entry_level: str, min_level: str | None) -> bool:
    """Check if a log entry passes the minimum level filter."""
    if not min_level:
        return True
    min_ord = _LEVEL_ORDER.get(min_level.upper(), 0)
    entry_ord = _LEVEL_ORDER.get(entry_level.upper(), 0)
    return entry_ord >= min_ord


def _read_recent_lines(limit: int = 200, level: str | None = None) -> list[dict[str, str]]:
    """Read recent log lines from daemon.log."""
    log_path = _daemon_log_path()
    if not log_path.is_file():
        return []

    lines: list[dict[str, str]] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()

        # Parse all lines, then take the last N that pass the filter
        for raw in raw_lines:
            parsed = _parse_log_line(raw)
            if parsed is None:
                continue
            if _passes_level_filter(parsed["level"], level):
                lines.append(parsed)

        return lines[-limit:]
    except OSError:
        return []


@router.get("/api/logs/recent")
async def logs_recent(
    limit: int = Query(200, ge=1, le=1000),
    level: str | None = Query(None),
) -> dict[str, Any]:
    """Return recent log lines as a non-streaming fallback."""
    lines = _read_recent_lines(limit=limit, level=level)
    return {"lines": lines, "total": len(lines)}


@ws_router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket) -> None:
    """Stream daemon log lines over WebSocket.

    Client can send filter messages: {"level": "WARN"} to filter server-side.
    Requires token via query parameter: /ws/logs?token=<token>
    """
    # Authenticate WebSocket via query parameter
    expected_token = websocket.app.state.dashboard_token
    client_token = websocket.query_params.get("token")
    if not client_token or client_token != expected_token:
        await websocket.close(code=4401, reason="unauthorized")
        return

    await websocket.accept()

    log_path = _daemon_log_path()
    level_filter: str | None = None

    # Send last 50 lines as initial backfill
    initial_lines = _read_recent_lines(limit=50, level=None)
    for entry in initial_lines:
        try:
            await websocket.send_json(entry)
        except WebSocketDisconnect:
            return

    # Tail the log file for new lines
    try:
        # Track file position
        file_pos = 0
        file_inode = 0
        if log_path.is_file():
            stat = log_path.stat()
            file_pos = stat.st_size
            file_inode = stat.st_ino

        while True:
            # Check for client messages (non-blocking)
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
                if isinstance(msg, dict) and "level" in msg:
                    level_filter = msg["level"] if msg["level"] else None
                continue
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                return

            # Check for new log lines
            if not log_path.is_file():
                continue

            try:
                stat = log_path.stat()
            except OSError:
                continue

            # Handle log rotation (inode changed or file shrunk)
            if stat.st_ino != file_inode or stat.st_size < file_pos:
                file_pos = 0
                file_inode = stat.st_ino

            if stat.st_size <= file_pos:
                continue

            # Read new lines
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(file_pos)
                    new_data = f.read()
                    file_pos = f.tell()
            except OSError:
                continue

            for line in new_data.splitlines():
                parsed = _parse_log_line(line)
                if parsed is None:
                    continue
                if not _passes_level_filter(parsed["level"], level_filter):
                    continue
                try:
                    await websocket.send_json(parsed)
                except WebSocketDisconnect:
                    return

    except WebSocketDisconnect:
        pass
    except Exception:
        # Graceful close on any unexpected error
        try:
            await websocket.close()
        except Exception:
            pass
