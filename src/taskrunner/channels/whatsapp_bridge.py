"""WhatsApp bridge abstractions — protocol adapters for sending/receiving messages."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class WhatsAppMessage:
    """A single inbound WhatsApp message."""

    sender: str
    text: str
    timestamp: datetime
    message_id: str


class WhatsAppBridge(ABC):
    """Abstract bridge to a WhatsApp protocol implementation.

    Concrete implementations either run in-process (neonize) or talk
    to a containerised bridge over HTTP.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to WhatsApp."""

    @abstractmethod
    def disconnect(self) -> None:
        """Gracefully disconnect."""

    @abstractmethod
    def send_message(self, recipient: str, text: str) -> None:
        """Send a text message to a WhatsApp number/JID."""

    @abstractmethod
    def get_messages_since(self, since: datetime) -> list[WhatsAppMessage]:
        """Return messages received after *since*."""

    @abstractmethod
    def get_latest_timestamp(self) -> datetime:
        """Return the timestamp of the most recent message."""

    def health(self) -> dict:
        """Return bridge health status."""
        return {"healthy": True}


class HttpWhatsAppBridge(WhatsAppBridge):
    """Bridge that talks to a containerised WhatsApp process over HTTP.

    The container runs whatsmeow (Go) or Baileys (Node.js) with a thin
    REST API exposing ``/health``, ``/messages``, and ``/send`` endpoints.
    """

    def __init__(self, bridge_url: str) -> None:
        self._url = bridge_url.rstrip("/")
        self._connected = False

    def connect(self) -> None:
        import httpx

        resp = httpx.get(f"{self._url}/health", timeout=5)
        resp.raise_for_status()
        self._connected = True
        logger.info("Connected to WhatsApp bridge at %s", self._url)

    def disconnect(self) -> None:
        self._connected = False

    def send_message(self, recipient: str, text: str) -> None:
        import httpx

        resp = httpx.post(
            f"{self._url}/send",
            json={"recipient": recipient, "text": text},
            timeout=30,
        )
        resp.raise_for_status()

    def get_messages_since(self, since: datetime) -> list[WhatsAppMessage]:
        import httpx

        resp = httpx.get(
            f"{self._url}/messages",
            params={"since": since.isoformat()},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        messages = []
        for m in data.get("messages", []):
            messages.append(
                WhatsAppMessage(
                    sender=m["sender"],
                    text=m["text"],
                    timestamp=datetime.fromisoformat(m["timestamp"]),
                    message_id=m["message_id"],
                )
            )
        return messages

    def get_latest_timestamp(self) -> datetime:
        import httpx

        resp = httpx.get(f"{self._url}/messages", params={"limit": 1}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        msgs = data.get("messages", [])
        if msgs:
            return datetime.fromisoformat(msgs[0]["timestamp"])
        return datetime.now(timezone.utc)

    def health(self) -> dict:
        try:
            import httpx

            resp = httpx.get(f"{self._url}/health", timeout=5)
            return {"healthy": resp.status_code == 200, "bridge_url": self._url}
        except Exception as exc:
            return {"healthy": False, "bridge_url": self._url, "error": str(exc)}


class NeonizeWhatsAppBridge(WhatsAppBridge):
    """In-process bridge using neonize (Python whatsmeow bindings).

    Suitable for development/simple setups. Handles QR code pairing
    and persists auth state to disk.
    """

    def __init__(self, auth_state_dir: str) -> None:
        self._auth_state_dir = auth_state_dir
        self._client = None

    def connect(self) -> None:
        try:
            import neonize  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "neonize is required for in-process WhatsApp bridge. "
                "Install with: uv pip install -e '.[whatsapp]'"
            ) from exc

        logger.info(
            "Neonize bridge: auth_state_dir=%s (connect is a stub — "
            "implement with real neonize API)",
            self._auth_state_dir,
        )

    def disconnect(self) -> None:
        logger.info("Neonize bridge disconnected")

    def send_message(self, recipient: str, text: str) -> None:
        raise NotImplementedError("Neonize send_message not yet implemented")

    def get_messages_since(self, since: datetime) -> list[WhatsAppMessage]:
        raise NotImplementedError("Neonize get_messages_since not yet implemented")

    def get_latest_timestamp(self) -> datetime:
        return datetime.now(timezone.utc)
