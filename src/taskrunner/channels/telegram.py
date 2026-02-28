"""Telegram channel — sends/receives messages via the Telegram Bot API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from taskrunner.channels.base import Channel, IncomingMessage, LegacyCallback
from taskrunner.channels.mixins import BridgeClientMixin, PollingChannelMixin
from taskrunner.channels.telegram_bridge import (
    HttpTelegramBridge,
    TelegramBridge,
    TelegramMessage,
)
from taskrunner.channels.webhook import WebhookChannelMixin

if TYPE_CHECKING:
    from taskrunner.channels.plugin import ChannelPluginMeta

logger = logging.getLogger(__name__)


class TelegramChannel(PollingChannelMixin, BridgeClientMixin, WebhookChannelMixin, Channel):
    """Telegram messaging channel.

    Supports two operating modes:
    - **polling**: long-polls the Bot API via getUpdates.
    - **webhook**: blocks in ``listen()`` and relies on FastAPI webhook
      routes (mounted via ``get_webhook_routes()``) to push messages.
    """

    def __init__(
        self,
        bridge: TelegramBridge,
        *,
        mode: str = "polling",
        poll_timeout: int = 30,
        allowed_senders: list[str] | None = None,
        allowed_chats: list[str] | None = None,
        webhook_path: str = "/webhooks/telegram",
        webhook_secret: str = "",
        send_typing: bool = True,
    ) -> None:
        self._bridge = bridge
        self._mode = mode
        self._poll_timeout = poll_timeout
        self._allowed_senders = set(allowed_senders or [])
        self._allowed_chats = set(allowed_chats or [])
        self._webhook_path = webhook_path
        self._webhook_secret = webhook_secret
        self._send_typing = send_typing

        if mode == "webhook" and not webhook_secret:
            logger.warning(
                "Telegram webhook mode with no secret — "
                "any sender can push updates to the webhook endpoint"
            )
        self._callback: LegacyCallback | None = None
        self._bot_username: str = ""
        self._offset: int | None = None

        # Build allowed outbound recipients from numeric sender IDs + chat IDs
        self._allowed_recipients: set[str] = set(allowed_chats or [])
        for s in allowed_senders or []:
            if not s.startswith("@"):
                self._allowed_recipients.add(s)

    # --- Channel interface ---

    def listen(self, callback: LegacyCallback) -> None:
        self._callback = callback

        # Cache bot username for @mention detection in groups
        bot_info = self._bridge.get_me()
        self._bot_username = bot_info.get("username", "")

        if self._mode == "webhook":
            self.set_webhook_callback(callback)
            logger.info("Telegram channel listening in webhook mode")
            self._webhook_listen_block()
        else:
            # Clear any existing webhook before polling
            self._bridge.delete_webhook()
            logger.info("Telegram channel listening in polling mode")
            self._run_poll_loop(callback)

        logger.info("Telegram channel stopped")

    def send(self, recipient: str, text: str) -> None:
        if not text:
            logger.debug("Skipping empty message to %s", recipient)
            return
        if recipient not in self._allowed_recipients:
            logger.warning("Blocked outbound message to %s — not in allowed recipients", recipient)
            return
        self._bridge.send_message(recipient, text)
        logger.info("Sent Telegram message to %s (%d chars)", recipient, len(text))

    def stop(self) -> None:
        self._stop_requested = True

    # --- PollingChannelMixin implementation ---

    def _poll_once(self) -> list[IncomingMessage]:
        raw_messages = self._bridge.get_updates(offset=self._offset, timeout=self._poll_timeout)
        result: list[IncomingMessage] = []

        for msg in raw_messages:
            # Advance offset past this update
            self._offset = msg.update_id + 1

            if not self._is_allowed(msg):
                continue

            self._allowed_recipients.add(msg.chat_id)

            text = self._extract_text(msg)
            if text is None:
                continue

            logger.info("Telegram from %s (@%s)", msg.sender_id, msg.sender_username)
            logger.debug("Telegram message text: %s", text[:80])

            result.append(
                IncomingMessage(
                    sender_id=msg.chat_id,
                    text=text,
                    channel="telegram",
                    group_id=msg.chat_id if msg.is_group else None,
                    metadata={
                        "sender_user_id": msg.sender_id,
                        "sender_username": msg.sender_username,
                        "message_id": msg.message_id,
                        "update_id": msg.update_id,
                    },
                )
            )

        return result

    def _before_dispatch(self, msg: IncomingMessage) -> None:
        """Send a typing indicator just before the LLM callback runs."""
        if self._send_typing:
            self._bridge.send_typing(msg.sender_id)

    # --- Access control ---

    def _is_allowed(self, msg: TelegramMessage) -> bool:
        """Check if a message passes sender/chat access control."""
        if not self._allowed_senders:
            return False

        sender_ok = (
            msg.sender_id in self._allowed_senders
            or msg.sender_username in self._allowed_senders
            or f"@{msg.sender_username}" in self._allowed_senders
        )
        if not sender_ok:
            return False

        if self._allowed_chats and msg.is_group:
            if msg.chat_id not in self._allowed_chats:
                return False

        return True

    # --- Group mention handling ---

    def _extract_text(self, msg: TelegramMessage) -> str | None:
        """Extract processable text from a message.

        In group chats, only processes messages mentioning @botusername.
        Strips the mention from the text. DMs are always processed.
        """
        text = msg.text
        if not text:
            return None

        if msg.is_group and self._bot_username:
            mention = f"@{self._bot_username}"
            if mention.lower() not in text.lower():
                return None
            # Strip the mention (case-insensitive)
            text = re.sub(re.escape(mention), "", text, flags=re.IGNORECASE).strip()

        return text if text else None

    # --- Webhook mode ---

    def get_webhook_routes(self) -> list[dict[str, Any]] | None:
        if self._mode != "webhook":
            return None

        return [
            {
                "path": self._webhook_path,
                "method": "POST",
                "handler": self._handle_webhook,
            },
        ]

    async def _handle_webhook(self, request) -> dict:
        """Handle incoming Telegram webhook update."""
        from fastapi import HTTPException

        raw_body = await request.body()

        # Secret token verification
        if self._webhook_secret:
            token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(token_header, self._webhook_secret):
                raise HTTPException(status_code=403, detail="Invalid secret token")

        body = json.loads(raw_body)

        # Parse the update
        msg_data = body.get("message")
        if not msg_data:
            return {"status": "ok"}

        chat = msg_data.get("chat", {})
        sender = msg_data.get("from", {})
        chat_type = chat.get("type", "private")
        text = msg_data.get("text", "") or msg_data.get("caption", "")
        chat_id = str(chat.get("id", ""))

        if not text:
            return {"status": "ok"}

        msg = TelegramMessage(
            sender_id=str(sender.get("id", "")),
            sender_username=sender.get("username", ""),
            chat_id=chat_id,
            text=text,
            update_id=body.get("update_id", 0),
            is_group=chat_type in ("group", "supergroup"),
            message_id=msg_data.get("message_id", 0),
        )

        if not self._is_allowed(msg):
            return {"status": "ok"}

        self._allowed_recipients.add(msg.chat_id)

        processed_text = self._extract_text(msg)
        if processed_text is None:
            return {"status": "ok"}

        callback = self._webhook_callback or self._callback
        if callback:
            if self._send_typing:
                self._bridge.send_typing(chat_id)
            response = await asyncio.to_thread(callback, chat_id, processed_text)
            await asyncio.to_thread(self.send, chat_id, response)

        return {"status": "ok"}

    # --- Health ---

    def health_check(self) -> dict[str, Any]:
        return self._bridge_health_check()


def register_plugin() -> tuple[ChannelPluginMeta, Callable[[dict[str, Any]], Channel]]:
    """Return plugin metadata and factory for the Telegram channel."""
    from taskrunner.channels.plugin import ChannelCapability, ChannelPluginMeta
    from taskrunner.models import TelegramChannelConfig

    meta = ChannelPluginMeta(
        id="telegram",
        label="Telegram",
        capabilities=(
            ChannelCapability.POLLING
            | ChannelCapability.WEBHOOK
            | ChannelCapability.SEND
            | ChannelCapability.TYPING_INDICATOR
            | ChannelCapability.GROUP_CHAT
        ),
        config_schema=TelegramChannelConfig,
    )

    def factory(config: dict[str, Any]) -> TelegramChannel:
        # Decrypt secrets into env before config expansion (same pattern as tools/LLM)
        secrets_path = config.get("secrets")
        if secrets_path:
            from taskrunner.secrets import decrypt_env_file

            for k, v in decrypt_env_file(secrets_path).items():
                os.environ[k] = v

        cfg = TelegramChannelConfig(**config)
        bridge = HttpTelegramBridge(cfg.bot_token)
        return TelegramChannel(
            bridge=bridge,
            mode=cfg.mode,
            poll_timeout=cfg.poll_timeout,
            allowed_senders=cfg.allowed_senders,
            allowed_chats=cfg.allowed_chats,
            webhook_path=cfg.webhook_path,
            webhook_secret=cfg.webhook_secret,
            send_typing=cfg.send_typing,
        )

    return meta, factory
