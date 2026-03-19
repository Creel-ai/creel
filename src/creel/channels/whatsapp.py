"""WhatsApp channel — sends/receives messages via a WhatsApp bridge."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from creel.channels.base import Channel
from creel.channels.mixins import HealthCheckMixin, RetryMixin
from creel.channels.sender_gate import SenderGate, SenderPolicy
from creel.channels.webhook import WebhookChannelMixin
from creel.channels.whatsapp_bridge import (
    HttpWhatsAppBridge,
    NeonizeWhatsAppBridge,
    WhatsAppBridge,
)

if TYPE_CHECKING:
    from creel.channels.plugin import ChannelPluginMeta

logger = logging.getLogger(__name__)


class WhatsAppChannel(HealthCheckMixin, RetryMixin, WebhookChannelMixin, Channel):
    """WhatsApp messaging channel.

    Supports two operating modes:
    - **polling**: periodically queries the bridge for new messages.
    - **webhook**: blocks in ``listen()`` and relies on FastAPI webhook
      routes (mounted via ``get_webhook_routes()``) to push messages.
    """

    def __init__(
        self,
        bridge: WhatsAppBridge,
        *,
        phone_number: str,
        mode: str = "polling",
        poll_interval: int = 5,
        allowed_senders: list[str] | None = None,
        webhook_path: str = "/webhooks/whatsapp",
        webhook_verify_token: str = "",
        webhook_secret: str = "",
        sender_gate: SenderGate | None = None,
    ) -> None:
        self._bridge = bridge
        self._phone_number = phone_number
        self._mode = mode
        self._poll_interval = poll_interval
        self._allowed_senders = set(allowed_senders or [])
        self._webhook_path = webhook_path
        self._webhook_verify_token = webhook_verify_token
        self._webhook_secret = webhook_secret
        self._gate = sender_gate
        self._callback: Callable[[str, str], str] | None = None

    # --- Channel interface ---

    def listen(self, callback: Callable[[str, str], str]) -> None:
        self._callback = callback
        self._bridge.connect()

        if self._mode == "webhook":
            self.set_webhook_callback(callback)
            logger.info("WhatsApp channel listening in webhook mode")
            self._webhook_listen_block()
        else:
            logger.info("WhatsApp channel listening in polling mode")
            self._poll_loop(callback)

        self._bridge.disconnect()
        logger.info("WhatsApp channel stopped")

    def send(self, recipient: str, text: str) -> None:
        if not text:
            logger.debug("Skipping empty message to %s", recipient)
            return
        self._bridge.send_message(recipient, text)
        logger.info("Sent WhatsApp message to %s (%d chars)", recipient, len(text))

    def stop(self) -> None:
        self._stop_requested = True

    # --- Polling mode ---

    def _poll_loop(self, callback: Callable[[str, str], str]) -> None:
        last_ts = self._bridge.get_latest_timestamp()
        consecutive_errors = 0
        max_backoff = 60

        while not self._stop_requested:
            try:
                messages = self._bridge.get_messages_since(last_ts)
                consecutive_errors = 0

                for msg in messages:
                    if not self._check_sender(msg.sender, msg.text):
                        if msg.timestamp > last_ts:
                            last_ts = msg.timestamp
                        continue
                    # Intercept gate commands from owners
                    if self._gate is not None:
                        gate_reply = self._gate.handle_owner_response(msg.text, msg.sender)
                        if gate_reply is not None:
                            self.send(msg.sender, gate_reply)
                            self._replay_held_messages(msg.text, callback)
                            if msg.timestamp > last_ts:
                                last_ts = msg.timestamp
                            continue
                    logger.info("WhatsApp from %s: %s", msg.sender, msg.text[:80])
                    response = callback(msg.sender, msg.text)
                    self.send(msg.sender, response)
                    if msg.timestamp > last_ts:
                        last_ts = msg.timestamp

            except Exception:
                consecutive_errors += 1
                backoff = self._calculate_backoff(
                    self._poll_interval, consecutive_errors, max_backoff
                )
                logger.exception(
                    "Error polling WhatsApp (consecutive=%d, backoff=%.1fs)",
                    consecutive_errors,
                    backoff,
                )
                time.sleep(backoff)
                continue

            time.sleep(self._poll_interval)

    # --- Webhook mode ---

    def get_webhook_routes(self) -> list[dict[str, Any]] | None:
        if self._mode != "webhook":
            return None

        return [
            {
                "path": self._webhook_path,
                "method": "GET",
                "handler": self._handle_webhook_verify,
            },
            {
                "path": self._webhook_path,
                "method": "POST",
                "handler": self._handle_webhook,
            },
        ]

    async def _handle_webhook_verify(self, request) -> dict:
        """WhatsApp webhook verification challenge response."""
        from fastapi import HTTPException

        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")

        if mode == "subscribe" and token == self._webhook_verify_token:
            return {"hub.challenge": challenge}
        raise HTTPException(status_code=403, detail="Verification failed")

    async def _handle_webhook(self, request) -> dict:
        """Handle incoming WhatsApp webhook payload."""
        from fastapi import HTTPException

        raw_body = await request.body()

        # HMAC-SHA256 signature verification
        if self._webhook_secret:
            signature_header = request.headers.get("X-Hub-Signature-256", "")
            if not signature_header.startswith("sha256="):
                raise HTTPException(status_code=403, detail="Missing signature")
            expected = hmac.new(
                self._webhook_secret.encode(),
                raw_body,
                hashlib.sha256,
            ).hexdigest()
            received = signature_header[len("sha256=") :]
            if not hmac.compare_digest(expected, received):
                raise HTTPException(status_code=403, detail="Invalid signature")

        import json

        body = json.loads(raw_body)

        # Extract messages from the webhook payload (Meta/Cloud API format)
        entries = body.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    if msg.get("type") != "text":
                        logger.debug("Skipping non-text message type=%s", msg.get("type"))
                        continue
                    sender = msg.get("from", "")
                    text = msg.get("text", {}).get("body", "")
                    if not text:
                        continue

                    if not self._check_sender(sender, text):
                        continue

                    # Run callback in thread to avoid blocking the event loop
                    callback = self._webhook_callback or self._callback
                    if callback:
                        # Intercept gate commands from owners
                        if self._gate is not None:
                            gate_reply = self._gate.handle_owner_response(text, sender)
                            if gate_reply is not None:
                                await asyncio.to_thread(self.send, sender, gate_reply)
                                await asyncio.to_thread(self._replay_held_messages, text, callback)
                                continue
                        response = await asyncio.to_thread(callback, sender, text)
                        await asyncio.to_thread(self.send, sender, response)

        return {"status": "ok"}

    # --- Sender gating ---

    def _check_sender(self, sender: str, text: str = "") -> bool:
        """Return True if *sender* is allowed to interact."""
        if self._gate is not None:
            result = self._gate.check(sender, text=text)
            return result.allowed
        # Legacy: static list check
        if self._allowed_senders and sender not in self._allowed_senders:
            return False
        return True

    def _replay_held_messages(self, command_text: str, callback: Callable[[str, str], str]) -> None:
        if self._gate is None:
            return
        self._gate.replay_held(command_text, callback, self.send)

    # --- Health ---

    def health_check(self) -> dict[str, Any]:
        return self._bridge_health()


def register_plugin() -> tuple[ChannelPluginMeta, Callable[[dict[str, Any]], Channel]]:
    """Return plugin metadata and factory for the WhatsApp channel."""
    from creel.channels.plugin import ChannelCapability, ChannelPluginMeta
    from creel.models import WhatsAppChannelConfig

    meta = ChannelPluginMeta(
        id="whatsapp",
        label="WhatsApp",
        capabilities=(
            ChannelCapability.POLLING | ChannelCapability.WEBHOOK | ChannelCapability.SEND
        ),
        config_schema=WhatsAppChannelConfig,
        extras=["whatsapp"],
    )

    def factory(config: dict[str, Any]) -> WhatsAppChannel:
        cfg = WhatsAppChannelConfig(**config)

        # Choose bridge implementation
        bridge: WhatsAppBridge
        if cfg.bridge_url:
            bridge = HttpWhatsAppBridge(cfg.bridge_url)
        else:
            bridge = NeonizeWhatsAppBridge(cfg.auth_state_dir)

        gate: SenderGate | None = None
        if cfg.sender_policy != "closed":
            from creel.channels.sender_store import SenderStore

            store = SenderStore("sender_data", "whatsapp")
            owner_id = cfg.owner or (cfg.allowed_senders[0] if cfg.allowed_senders else "")
            owner_ids = {owner_id} if owner_id else set()

            def _notify(recipient: str, text: str) -> None:
                if cfg.notify_owner:
                    bridge.send_message(recipient, text)

            gate = SenderGate(
                policy=SenderPolicy(cfg.sender_policy),
                static_senders=set(cfg.allowed_senders),
                store=store,
                owner_sender_ids=owner_ids,
                notify_fn=_notify,
                auto_approve=cfg.auto_approve_senders,
            )

        return WhatsAppChannel(
            bridge=bridge,
            phone_number=cfg.phone_number,
            mode=cfg.mode,
            poll_interval=cfg.poll_interval,
            allowed_senders=cfg.allowed_senders,
            webhook_path=cfg.webhook_path,
            webhook_verify_token=cfg.webhook_verify_token,
            webhook_secret=cfg.webhook_secret,
            sender_gate=gate,
        )

    return meta, factory
