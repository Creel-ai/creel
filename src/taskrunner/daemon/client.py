"""Daemon API client helpers for CLI/TUI attach flows."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class RemoteSession:
    """Session snapshot fetched from daemon API."""

    sender_id: str
    session_id: str
    title: str = ""
    created_at: float = 0.0
    last_active: float = 0.0
    messages: list[dict[str, Any]] | None = None


class DaemonApiClient:
    """Thin HTTP-over-UDS client for daemon API.

    Reuses a single httpx.Client for the lifetime of the instance.
    Call close() when done, or use as a context manager.
    """

    def __init__(self, socket_path: str | Path, timeout: float = 300.0) -> None:
        self._socket_path = Path(socket_path)
        self._timeout = timeout
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            transport = httpx.HTTPTransport(uds=str(self._socket_path))
            self._client = httpx.Client(
                transport=transport,
                base_url="http://daemon",
                timeout=self._timeout,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    def send_message(
        self,
        sender_id: str,
        text: str,
        session_id: str | None = None,
        auto_approve: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sender_id": sender_id,
            "text": text,
        }
        if session_id:
            payload["session_id"] = session_id
        if auto_approve:
            payload["auto_approve"] = True
        return self._request("POST", "/v1/messages", json_body=payload)

    def stream_message(
        self,
        sender_id: str,
        text: str,
        session_id: str | None = None,
        auto_approve: bool = False,
    ):
        payload: dict[str, Any] = {
            "sender_id": sender_id,
            "text": text,
        }
        if session_id:
            payload["session_id"] = session_id
        if auto_approve:
            payload["auto_approve"] = True

        client = self._get_client()
        with client.stream("POST", "/v1/messages/stream", json=payload) as resp:
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = resp.json().get("detail", "")
                except Exception:
                    detail = resp.text
                raise RuntimeError(
                    f"Daemon API POST /v1/messages/stream failed ({resp.status_code}): {detail}"
                )

            event_type = ""
            data_lines: list[str] = []
            for line in resp.iter_lines():
                if line == "":
                    if not data_lines:
                        event_type = ""
                        continue
                    payload_text = "\n".join(data_lines)
                    parsed = json.loads(payload_text)
                    if event_type and "type" not in parsed:
                        parsed["type"] = event_type
                    yield parsed
                    event_type = ""
                    data_lines = []
                    continue

                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].strip())

            if data_lines:
                payload_text = "\n".join(data_lines)
                parsed = json.loads(payload_text)
                if event_type and "type" not in parsed:
                    parsed["type"] = event_type
                yield parsed

    def get_active_session(self, sender_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            "/v1/sessions/active",
            params={"sender_id": sender_id},
        )

    def list_sessions(self, sender_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/v1/sessions",
            params={"sender_id": sender_id},
        )

    def new_session(self, sender_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/sessions/new",
            json_body={"sender_id": sender_id},
        )

    def resume_session(self, sender_id: str, session_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{session_id}/resume",
            json_body={"sender_id": sender_id},
        )

    def get_history(
        self,
        sender_id: str,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/v1/sessions/{session_id}/history",
            params={"sender_id": sender_id, "limit": limit},
        )
        return payload.get("messages", [])

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        client = self._get_client()
        resp = client.request(method, path, json=json_body, params=params)

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = resp.text
            raise RuntimeError(
                f"Daemon API {method} {path} failed ({resp.status_code}): {detail}"
            )

        if not resp.text:
            return {}
        return resp.json()


class DaemonTuiAdapter:
    """Adapter that exposes a ChatServer-like surface for the Textual TUI."""

    def __init__(self, client: DaemonApiClient, sender_id: str = "cli") -> None:
        self._client = client
        self._sender_id = sender_id
        self._active_session_id: str | None = None

    def handle_message(
        self,
        sender_id: str,
        text: str,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> str:
        if on_text_delta is None:
            response = self._client.send_message(
                sender_id=sender_id,
                text=text,
                session_id=self._active_session_id,
            )
            session_id = response.get("session_id")
            if isinstance(session_id, str) and session_id:
                self._active_session_id = session_id
            return str(response.get("text", ""))

        final_text = ""
        got_final = False
        for event in self.stream_message(sender_id, text):
            event_type = str(event.get("type", ""))
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}

            if event_type == "token":
                chunk = str(payload.get("text", ""))
                if chunk:
                    on_text_delta(chunk)
            elif event_type == "final":
                final_text = str(payload.get("text", ""))
                got_final = True
            elif event_type == "error":
                err = payload.get("error", "streaming request failed")
                raise RuntimeError(str(err))
        if not got_final:
            logger.warning("stream ended without a final event")
        return final_text

    def stream_message(self, sender_id: str, text: str):
        for event in self._client.stream_message(
            sender_id=sender_id,
            text=text,
            session_id=self._active_session_id,
        ):
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                self._active_session_id = session_id
            yield event

    def get_or_create_session(self, sender_id: str) -> RemoteSession:
        summary = self._client.get_active_session(sender_id)
        session_id = str(summary["session_id"])
        self._active_session_id = session_id
        messages = self._client.get_history(sender_id, session_id, limit=200)
        return RemoteSession(
            sender_id=str(summary.get("sender_id", sender_id)),
            session_id=session_id,
            title=str(summary.get("title") or ""),
            created_at=float(summary.get("created_at", 0.0) or 0.0),
            last_active=float(summary.get("last_active", 0.0) or 0.0),
            messages=messages,
        )

    def new_session(self, sender_id: str) -> RemoteSession:
        summary = self._client.new_session(sender_id)
        session_id = str(summary["session_id"])
        self._active_session_id = session_id
        return RemoteSession(
            sender_id=str(summary.get("sender_id", sender_id)),
            session_id=session_id,
            title=str(summary.get("title") or ""),
            created_at=float(summary.get("created_at", 0.0) or 0.0),
            last_active=float(summary.get("last_active", 0.0) or 0.0),
            messages=[],
        )

    def resume_session(self, sender_id: str, session_id: str) -> RemoteSession:
        summary = self._client.resume_session(sender_id, session_id)
        self._active_session_id = str(summary["session_id"])
        messages = self._client.get_history(
            sender_id, self._active_session_id, limit=200
        )
        return RemoteSession(
            sender_id=str(summary.get("sender_id", sender_id)),
            session_id=self._active_session_id,
            title=str(summary.get("title") or ""),
            created_at=float(summary.get("created_at", 0.0) or 0.0),
            last_active=float(summary.get("last_active", 0.0) or 0.0),
            messages=messages,
        )

    def list_sessions_text(self, sender_id: str) -> str:
        sessions = self._client.list_sessions(sender_id)
        if not sessions:
            return "No sessions found."

        active = self._active_session_id
        lines = ["Sessions:", ""]
        for s in sessions:
            sid = str(s.get("session_id", ""))
            title = str(s.get("title") or "(untitled)")
            count = int(s.get("message_count", 0) or 0)
            marker = " *" if active and sid == active else ""
            lines.append(f"  {sid}{marker}  {title} ({count} msgs)")
        lines.append("")
        lines.append("* = active session. Use /resume <id> to switch.")
        return "\n".join(lines)
