"""Telegram Bot API bridge abstractions — protocol adapters for sending/receiving messages."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Telegram sendMessage limit
MAX_MESSAGE_LENGTH = 4096


@dataclass
class TelegramMessage:
    """A single inbound Telegram message."""

    sender_id: str
    sender_username: str
    chat_id: str
    text: str
    update_id: int
    is_group: bool
    message_id: int
    media: list[TelegramMedia] | None = None


@dataclass
class TelegramMedia:
    """Media attachment from an inbound Telegram message."""

    file_id: str
    file_type: str  # "photo", "document", "voice", "video"
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


class TelegramBridge(ABC):
    """Abstract bridge to the Telegram Bot API."""

    @abstractmethod
    def get_me(self) -> dict:
        """Return bot info (id, username, etc.)."""

    @abstractmethod
    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[TelegramMessage]:
        """Long-poll for new messages via getUpdates."""

    @abstractmethod
    def send_message(self, chat_id: str, text: str, reply_to_message_id: int | None = None) -> None:
        """Send a text message, chunking if necessary."""

    @abstractmethod
    def send_typing(self, chat_id: str) -> None:
        """Send 'typing' chat action."""

    @abstractmethod
    def set_webhook(self, url: str, secret_token: str = "") -> None:
        """Register a webhook URL with Telegram."""

    @abstractmethod
    def delete_webhook(self) -> None:
        """Remove the current webhook."""

    @abstractmethod
    def download_file(self, file_id: str) -> bytes:
        """Download a file by file_id and return its contents.

        The download URL contains the bot token, so this method fetches the
        file server-side and returns raw bytes — the URL is never exposed to
        callers (and must never be passed to the LLM).
        """

    def health(self) -> dict:
        """Return bridge health status."""
        return {"healthy": True}


class HttpTelegramBridge(TelegramBridge):
    """Concrete bridge that calls the Telegram Bot API over HTTPS."""

    def __init__(self, bot_token: str, api_base_url: str | None = None) -> None:
        self._token = bot_token
        self._api_base_url = (api_base_url or "https://api.telegram.org").rstrip("/")
        self._base_url = f"{self._api_base_url}/bot{bot_token}"
        self._bot_info: dict | None = None

    def _call(self, method: str, **kwargs) -> dict:
        """Call a Bot API method and return the result."""
        import httpx

        resp = httpx.post(
            f"{self._base_url}/{method}",
            json=kwargs,
            timeout=max(kwargs.get("timeout", 0) + 5, 30),
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error on {method}: {data.get('description', data)}")
        return data.get("result", {})

    def get_me(self) -> dict:
        if self._bot_info is None:
            self._bot_info = self._call("getMe")
            logger.info(
                "Telegram bot: @%s (id=%s)",
                self._bot_info.get("username"),
                self._bot_info.get("id"),
            )
        return self._bot_info

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[TelegramMessage]:
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        # Telegram holds the connection for `timeout` seconds
        result = self._call("getUpdates", **params)
        messages = []
        updates: list[dict] = result if isinstance(result, list) else []
        for update in updates:
            msg = self._parse_message(update)
            if msg is not None:
                messages.append(msg)
        return messages

    def send_message(self, chat_id: str, text: str, reply_to_message_id: int | None = None) -> None:
        for chunk in _chunk_text(text, MAX_MESSAGE_LENGTH):
            params: dict = {"chat_id": chat_id, "text": chunk}
            if reply_to_message_id is not None:
                params["reply_to_message_id"] = reply_to_message_id
                reply_to_message_id = None  # only reply to the first chunk
            self._call("sendMessage", **params)

    def send_typing(self, chat_id: str) -> None:
        self._call("sendChatAction", chat_id=chat_id, action="typing")

    def set_webhook(self, url: str, secret_token: str = "") -> None:
        params: dict = {"url": url}
        if secret_token:
            params["secret_token"] = secret_token
        self._call("setWebhook", **params)
        logger.info("Telegram webhook set to %s", url)

    def delete_webhook(self) -> None:
        self._call("deleteWebhook")
        logger.info("Telegram webhook deleted")

    def download_file(self, file_id: str) -> bytes:
        import httpx

        result = self._call("getFile", file_id=file_id)
        file_path = result.get("file_path", "")
        url = f"{self._api_base_url}/file/bot{self._token}/{file_path}"
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    def health(self) -> dict:
        try:
            self.get_me()
            return {"healthy": True}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    def _parse_message(self, update: dict) -> TelegramMessage | None:
        """Extract a TelegramMessage from a raw update dict."""
        msg = update.get("message")
        if msg is None:
            return None

        chat = msg.get("chat", {})
        sender = msg.get("from", {})
        chat_type = chat.get("type", "private")

        # Extract text; fall back to caption for media messages
        text = msg.get("text", "") or msg.get("caption", "")

        # Extract media attachments
        media = _extract_media(msg)

        return TelegramMessage(
            sender_id=str(sender.get("id", "")),
            sender_username=sender.get("username", ""),
            chat_id=str(chat.get("id", "")),
            text=text,
            update_id=update.get("update_id", 0),
            is_group=chat_type in ("group", "supergroup"),
            message_id=msg.get("message_id", 0),
            media=media or None,
        )


def _extract_media(msg: dict) -> list[TelegramMedia]:
    """Extract media attachments from a raw Telegram message dict."""
    media: list[TelegramMedia] = []

    # Photos: Telegram sends an array of sizes; pick the largest
    photos = msg.get("photo")
    if photos:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        media.append(
            TelegramMedia(
                file_id=best["file_id"],
                file_type="photo",
                mime_type="image/jpeg",  # Telegram photos are always JPEG
            )
        )

    # Voice messages
    voice = msg.get("voice")
    if voice:
        media.append(
            TelegramMedia(
                file_id=voice["file_id"],
                file_type="voice",
                mime_type=voice.get("mime_type", "audio/ogg"),
                file_size=voice.get("file_size"),
            )
        )

    # Audio files (music, podcasts, etc.)
    audio = msg.get("audio")
    if audio:
        media.append(
            TelegramMedia(
                file_id=audio["file_id"],
                file_type="audio",
                file_name=audio.get("file_name"),
                mime_type=audio.get("mime_type"),
                file_size=audio.get("file_size"),
            )
        )

    # Videos
    video = msg.get("video")
    if video:
        media.append(
            TelegramMedia(
                file_id=video["file_id"],
                file_type="video",
                file_name=video.get("file_name"),
                mime_type=video.get("mime_type", "video/mp4"),
                file_size=video.get("file_size"),
            )
        )

    # Documents (files sent as documents)
    document = msg.get("document")
    if document:
        mime = document.get("mime_type", "")
        # Classify based on MIME type
        if mime.startswith("image/"):
            file_type = "photo"
        elif mime.startswith("video/"):
            file_type = "video"
        else:
            file_type = "document"
        media.append(
            TelegramMedia(
                file_id=document["file_id"],
                file_type=file_type,
                file_name=document.get("file_name"),
                mime_type=mime or None,
                file_size=document.get("file_size"),
            )
        )

    return media


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most *limit* chars, breaking at newlines when possible."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to break at a newline within the limit
        break_at = text.rfind("\n", 0, limit)
        if break_at <= 0:
            break_at = limit
        chunks.append(text[:break_at])
        text = text[break_at:].lstrip("\n")
    return chunks
