#!/usr/bin/env python3
"""BlueBubbles executor — iMessage integration via BlueBubbles REST API.

Security: enforces allowlists, rate limits, and caps independent of Guardian.
All configuration comes from environment variables, never from LLM input.

Env vars:
    BLUEBUBBLES_URL       — BlueBubbles server URL (e.g. http://localhost:1234)
    BLUEBUBBLES_PASSWORD  — API password
    ALLOWED_RECIPIENTS    — comma-separated chat IDs/handles for sending
    ALLOWED_CHATS         — comma-separated chat IDs for reading (empty = all)
    ACTION                — which function to call (get_recent_messages, send_message, etc.)
    + action-specific args passed as env vars
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests


def register_skill():
    """Register the bluebubbles skill with the skill registry."""
    import json
    import os
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="bluebubbles",
        label="BlueBubbles",
        tools=(
            ToolSpec(
                name="check_messages",
                description="Get recent iMessages via BlueBubbles",
                params=(
                    Param(
                        name="chat_id",
                        type="string",
                        description="Chat ID to get messages from",
                    ),
                    Param(
                        name="limit",
                        type="string",
                        description="Maximum number of messages to return",
                    ),
                    Param(
                        name="after_date",
                        type="string",
                        description="Only return messages after this date",
                    ),
                ),
                fixed_args={"_action": "get_recent_messages"},
            ),
            ToolSpec(
                name="send_imessage",
                description="Send an iMessage via BlueBubbles",
                params=(
                    Param(
                        name="chat_id",
                        type="string",
                        description="Chat ID to send the message to",
                        required=True,
                    ),
                    Param(
                        name="text",
                        type="string",
                        description="Message text to send",
                        required=True,
                    ),
                ),
                fixed_args={"_action": "send_message"},
            ),
            ToolSpec(
                name="react_imessage",
                description="React to an iMessage via BlueBubbles",
                params=(
                    Param(
                        name="chat_id",
                        type="string",
                        description="Chat ID containing the message",
                        required=True,
                    ),
                    Param(
                        name="message_guid",
                        type="string",
                        description="GUID of the message to react to",
                        required=True,
                    ),
                    Param(
                        name="reaction",
                        type="string",
                        description="Reaction type (love, like, dislike, laugh, emphasis, question)",
                        required=True,
                    ),
                ),
                fixed_args={"_action": "send_reaction"},
            ),
            ToolSpec(
                name="get_chats",
                description="List recent iMessage chats via BlueBubbles",
                params=(
                    Param(
                        name="limit",
                        type="string",
                        description="Maximum number of chats to return",
                    ),
                ),
                fixed_args={"_action": "get_chats"},
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        action = config.args.get("_action", "")
        server_url = os.environ.get("BLUEBUBBLES_URL", "")
        password = os.environ.get("BLUEBUBBLES_PASSWORD", "")
        allowed_recipients = {
            v.strip() for v in os.environ.get("ALLOWED_RECIPIENTS", "").split(",") if v.strip()
        }
        allowed_chats = {
            v.strip() for v in os.environ.get("ALLOWED_CHATS", "").split(",") if v.strip()
        }

        result: object
        if action == "get_recent_messages":
            result = get_recent_messages(
                server_url,
                password,
                allowed_chats,
                chat_id=config.args.get("chat_id") or None,
                limit=int(config.args.get("limit", "20")),
                after_date=config.args.get("after_date") or None,
            )
        elif action == "send_message":
            result = send_message(
                server_url,
                password,
                allowed_recipients,
                chat_id=config.args.get("chat_id", ""),
                text=config.args.get("text", ""),
            )
        elif action == "send_reaction":
            result = send_reaction(
                server_url,
                password,
                allowed_recipients,
                chat_id=config.args.get("chat_id", ""),
                message_guid=config.args.get("message_guid", ""),
                reaction=config.args.get("reaction", ""),
            )
        elif action == "get_chats":
            result = get_chats(
                server_url,
                password,
                allowed_chats,
                limit=int(config.args.get("limit", "20")),
            )
        else:
            raise ValueError(f"Unknown bluebubbles action: {action}")

        return json.dumps(result, indent=2)

    return meta, execute


# ---------------------------------------------------------------------------
# Constants — hard caps enforced regardless of input
# ---------------------------------------------------------------------------
MAX_MESSAGES_PER_REQUEST = 50
MAX_CHATS_PER_REQUEST = 50
MAX_SEND_RATE = 10  # per minute
MAX_MESSAGE_LENGTH = 2000
MAX_MESSAGE_CONTENT_LENGTH = 1000  # truncate message bodies in responses
VALID_REACTIONS = {"love", "like", "dislike", "laugh", "emphasis", "question"}

# Simple in-memory rate limiter (per container invocation is single-shot,
# but we keep this for inline mode where the module stays loaded).
_send_timestamps: list[float] = []


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_set(name: str) -> set[str]:
    raw = _env(name)
    if not raw:
        return set()
    return {v.strip() for v in raw.split(",") if v.strip()}


def _api(method: str, path: str, server_url: str, password: str, **kwargs) -> dict:
    """Make an authenticated BlueBubbles API call."""
    url = f"{server_url}/api/v1{path}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {password}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _check_rate_limit() -> None:
    """Enforce send rate limit."""
    now = time.time()
    cutoff = now - 60
    _send_timestamps[:] = [t for t in _send_timestamps if t > cutoff]
    if len(_send_timestamps) >= MAX_SEND_RATE:
        raise RuntimeError(f"Rate limit exceeded: max {MAX_SEND_RATE} sends per minute")
    _send_timestamps.append(now)


def _check_recipient(chat_id: str, allowed: set[str]) -> None:
    """Ensure chat_id is in the recipient allowlist."""
    if not allowed:
        raise RuntimeError("No allowed recipients configured — sending disabled")
    if chat_id not in allowed:
        raise RuntimeError(f"Recipient '{chat_id}' not in allowlist")


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------


def get_recent_messages(
    server_url: str,
    password: str,
    allowed_chats: set[str],
    chat_id: str | None = None,
    limit: int = 20,
    after_date: str | None = None,
) -> list[dict]:
    """Get recent messages, respecting caps and allowlists.

    Returns list of {sender, text, date, chat_id, is_from_me}.
    Intentionally omits message GUIDs/IDs to prevent forwarding/export.
    """
    limit = max(1, min(limit, MAX_MESSAGES_PER_REQUEST))

    params: dict = {"limit": limit, "sort": "DESC"}
    if after_date:
        params["after"] = after_date

    if chat_id:
        # Validate chat access
        if allowed_chats and chat_id not in allowed_chats:
            raise RuntimeError(f"Chat '{chat_id}' not in allowed chats")
        data = _api("GET", f"/chat/{chat_id}/message", server_url, password, params=params)
    else:
        data = _api("GET", "/message", server_url, password, params=params)

    messages = []
    for msg in data.get("data", []):
        msg_chat_id = ""
        chats = msg.get("chats", [])
        if chats:
            msg_chat_id = chats[0].get("chatIdentifier", "")

        # Filter by allowed chats if configured
        if allowed_chats and msg_chat_id and msg_chat_id not in allowed_chats:
            continue

        text = msg.get("text", "") or ""
        if len(text) > MAX_MESSAGE_CONTENT_LENGTH:
            text = text[:MAX_MESSAGE_CONTENT_LENGTH] + "…"

        sender = ""
        handle = msg.get("handle", {})
        if handle:
            sender = handle.get("address", "")

        messages.append(
            {
                "sender": sender,
                "text": text,
                "date": msg.get("dateCreated", ""),
                "chat_id": msg_chat_id,
                "is_from_me": bool(msg.get("isFromMe", False)),
            }
        )

    return messages


def send_message(
    server_url: str,
    password: str,
    allowed_recipients: set[str],
    chat_id: str,
    text: str,
) -> dict:
    """Send a message. Enforces allowlist, rate limit, and length cap."""
    _check_recipient(chat_id, allowed_recipients)
    _check_rate_limit()

    if len(text) > MAX_MESSAGE_LENGTH:
        raise RuntimeError(f"Message too long ({len(text)} chars, max {MAX_MESSAGE_LENGTH})")

    _api(
        "POST",
        "/message/text",
        server_url,
        password,
        json={"chatGuid": chat_id, "message": text},
    )
    return {"status": "sent", "chat_id": chat_id}


def send_reaction(
    server_url: str,
    password: str,
    allowed_recipients: set[str],
    chat_id: str,
    message_guid: str,
    reaction: str,
) -> dict:
    """Send a tapback reaction. Validates reaction type and recipient."""
    _check_recipient(chat_id, allowed_recipients)
    _check_rate_limit()

    reaction = reaction.lower().strip()
    if reaction not in VALID_REACTIONS:
        raise RuntimeError(
            f"Invalid reaction '{reaction}'. Must be one of: {', '.join(sorted(VALID_REACTIONS))}"
        )

    _api(
        "POST",
        "/message/react",
        server_url,
        password,
        json={
            "chatGuid": chat_id,
            "selectedMessageGuid": message_guid,
            "reaction": reaction,
        },
    )
    return {"status": "reacted", "chat_id": chat_id, "reaction": reaction}


def get_chats(
    server_url: str,
    password: str,
    allowed_chats: set[str],
    limit: int = 20,
) -> list[dict]:
    """List recent chats (metadata only)."""
    limit = max(1, min(limit, MAX_CHATS_PER_REQUEST))

    data = _api(
        "GET", "/chat", server_url, password, params={"limit": limit, "sort": "lastmessage"}
    )

    chats = []
    for chat in data.get("data", []):
        chat_id = chat.get("chatIdentifier", "")
        if allowed_chats and chat_id not in allowed_chats:
            continue
        chats.append(
            {
                "chat_id": chat_id,
                "display_name": chat.get("displayName", "") or chat_id,
                "last_message_date": chat.get("lastMessage", {}).get("dateCreated", "")
                if chat.get("lastMessage")
                else "",
            }
        )

    return chats


# ---------------------------------------------------------------------------
# CLI entry point (used in container mode)
# ---------------------------------------------------------------------------


def main() -> None:
    server_url = _env("BLUEBUBBLES_URL")
    password = _env("BLUEBUBBLES_PASSWORD")
    action = _env("ACTION")
    allowed_recipients = _env_set("ALLOWED_RECIPIENTS")
    allowed_chats = _env_set("ALLOWED_CHATS")

    if not server_url or not password:
        print(
            json.dumps({"error": "BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD required"}),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result: list[dict[str, Any]] | dict[str, Any]
        if action == "get_recent_messages":
            chat_id = _env("CHAT_ID") or None
            limit = int(_env("LIMIT") or "20")
            after_date = _env("AFTER_DATE") or None
            result = get_recent_messages(
                server_url, password, allowed_chats, chat_id, limit, after_date
            )
        elif action == "send_message":
            chat_id = _env("CHAT_ID")
            text = _env("TEXT")
            if not chat_id or not text:
                raise RuntimeError("CHAT_ID and TEXT required for send_message")
            result = send_message(server_url, password, allowed_recipients, chat_id, text)
        elif action == "send_reaction":
            chat_id = _env("CHAT_ID")
            message_guid = _env("MESSAGE_GUID")
            reaction = _env("REACTION")
            if not all([chat_id, message_guid, reaction]):
                raise RuntimeError("CHAT_ID, MESSAGE_GUID, and REACTION required")
            result = send_reaction(
                server_url, password, allowed_recipients, chat_id, message_guid, reaction
            )
        elif action == "get_chats":
            limit = int(_env("LIMIT") or "20")
            result = get_chats(server_url, password, allowed_chats, limit)
        else:
            raise RuntimeError(f"Unknown action: {action}")

        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
