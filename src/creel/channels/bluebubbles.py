"""BlueBubbles channel — polls for incoming iMessages via BlueBubbles REST API."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import requests  # type: ignore[import-untyped]

from creel.channels import Channel
from creel.channels.mixins import RetryMixin
from creel.channels.sender_gate import SenderGate, SenderPolicy

if TYPE_CHECKING:
    from creel.channels.plugin import ChannelPluginMeta

logger = logging.getLogger(__name__)


class BlueBubblesChannel(RetryMixin, Channel):
    """iMessage channel via BlueBubbles server (REST polling)."""

    def __init__(
        self,
        server_url: str,
        password: str,
        allowed_senders: list[str],
        poll_interval: int = 3,
        sender_gate: SenderGate | None = None,
    ):
        self._server_url = server_url.rstrip("/")
        self._password = password
        self._allowed_senders = set(allowed_senders)
        self._poll_interval = poll_interval
        self._gate = sender_gate

    def _api(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._server_url}/api/v1{path}"
        params = kwargs.pop("params", {})
        params["password"] = self._password
        resp = requests.request(method, url, params=params, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def listen(self, callback: Callable[[str, str], str]) -> None:
        """Poll BlueBubbles for new messages and respond."""
        # Get latest message timestamp to only process new ones
        last_ts = self._get_latest_timestamp()
        logger.info(
            "BlueBubbles listener started (watching for: %s)",
            ", ".join(self._allowed_senders),
        )

        consecutive_errors = 0
        max_backoff = 60  # seconds

        while not self._stop_requested:
            try:
                messages = self._poll(last_ts)
                consecutive_errors = 0  # reset on success
                for msg in messages:
                    sender = msg["sender"]
                    ts = msg["timestamp"]
                    text = msg["text"]
                    if not self._check_sender(sender, text):
                        if ts > last_ts:
                            last_ts = ts
                        continue
                    # Intercept gate commands from owners
                    if self._gate is not None:
                        gate_reply = self._gate.handle_owner_response(text, sender)
                        if gate_reply is not None:
                            self.send(msg["chat_guid"], gate_reply)
                            self._replay_held_messages(text, callback)
                            if ts > last_ts:
                                last_ts = ts
                            continue
                    logger.info("Message from %s: %s", sender, text[:80])
                    response = callback(sender, text)
                    self.send(msg["chat_guid"], response)
                    if ts > last_ts:
                        last_ts = ts
            except Exception:
                consecutive_errors += 1
                backoff = self._calculate_backoff(
                    self._poll_interval, consecutive_errors, max_backoff
                )
                logger.exception(
                    "Error polling BlueBubbles (consecutive=%d, backoff=%.1fs)",
                    consecutive_errors,
                    backoff,
                )
                time.sleep(backoff)
                continue

            time.sleep(self._poll_interval)

        logger.info("BlueBubbles listener stopped")

    def send(self, recipient: str, text: str) -> None:
        """Send a message via BlueBubbles REST API."""
        if not text:
            logger.debug("Skipping empty message to %s", recipient)
            return
        self._api(
            "POST",
            "/message/text",
            json={"chatGuid": recipient, "message": text},
        )
        logger.info("Sent reply to %s (%d chars)", recipient, len(text))

    def _check_sender(self, sender: str, text: str = "") -> bool:
        if self._gate is not None:
            result = self._gate.check(sender, text=text)
            return result.allowed
        return sender in self._allowed_senders

    def _replay_held_messages(self, command_text: str, callback: Callable[[str, str], str]) -> None:
        if self._gate is None:
            return
        self._gate.replay_held(command_text, callback, self.send)

    def _get_latest_timestamp(self) -> int:
        """Get the timestamp of the most recent message."""
        try:
            data = self._api("GET", "/message", params={"limit": 1, "sort": "DESC"})
            messages = data.get("data", [])
            if messages:
                return messages[0].get("dateCreated", 0)
        except Exception:
            logger.warning("Could not get latest timestamp, starting from now")
        return int(time.time() * 1000)

    def _poll(self, after_ts: int) -> list[dict]:
        """Fetch messages newer than after_ts."""
        data = self._api(
            "GET",
            "/message",
            params={"limit": 20, "sort": "DESC", "after": after_ts},
        )

        messages = []
        for msg in data.get("data", []):
            if msg.get("isFromMe"):
                continue
            text = msg.get("text")
            if not text:
                continue

            handle = msg.get("handle", {}) or {}
            sender = handle.get("address", "")

            chat_guid = ""
            chats = msg.get("chats", [])
            if chats:
                chat_guid = chats[0].get("guid", "")

            messages.append(
                {
                    "sender": sender,
                    "text": text,
                    "chat_guid": chat_guid,
                    "timestamp": msg.get("dateCreated", 0),
                }
            )

        # Return oldest first
        messages.sort(key=lambda m: m["timestamp"])
        return messages


def register_plugin() -> tuple[ChannelPluginMeta, Callable[[dict[str, Any]], Channel]]:
    """Return plugin metadata and factory for the BlueBubbles channel."""
    from creel.channels.plugin import ChannelCapability, ChannelPluginMeta
    from creel.models import BlueBubblesChannelConfig

    meta = ChannelPluginMeta(
        id="bluebubbles",
        label="BlueBubbles",
        capabilities=ChannelCapability.POLLING | ChannelCapability.SEND,
        config_schema=BlueBubblesChannelConfig,
    )

    def factory(config: dict[str, Any]) -> BlueBubblesChannel:
        cfg = BlueBubblesChannelConfig(**config)

        gate: SenderGate | None = None
        if cfg.sender_policy != "closed":
            from creel.channels.sender_store import SenderStore

            store = SenderStore("sender_data", "bluebubbles")
            owner_id = cfg.owner or (cfg.listen_to[0] if cfg.listen_to else "")
            owner_ids = {owner_id} if owner_id else set()

            channel_ref: list[BlueBubblesChannel] = []

            def _notify(recipient: str, text: str) -> None:
                if cfg.notify_owner and channel_ref:
                    channel_ref[0].send(recipient, text)

            gate = SenderGate(
                policy=SenderPolicy(cfg.sender_policy),
                static_senders=set(cfg.listen_to),
                store=store,
                owner_sender_ids=owner_ids,
                notify_fn=_notify,
                auto_approve=cfg.auto_approve_senders,
            )

        ch = BlueBubblesChannel(
            server_url=cfg.server_url,
            password=cfg.password,
            allowed_senders=cfg.listen_to,
            poll_interval=cfg.poll_interval,
            sender_gate=gate,
        )
        if gate is not None:
            channel_ref.append(ch)
        return ch

    return meta, factory
