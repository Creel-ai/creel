"""iMessage channel - polls chat.db for incoming messages, replies via AppleScript."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from creel.channels import Channel
from creel.channels.message import Attachment, AttachmentType, IncomingMessage
from creel.outputs import MESSAGE_PREFIX

if TYPE_CHECKING:
    from creel.channels.plugin import ChannelPluginMeta

logger = logging.getLogger(__name__)

# Mapping MIME type prefixes to AttachmentType
_MIME_TO_ATTACHMENT_TYPE: dict[str, AttachmentType] = {
    "image/": AttachmentType.IMAGE,
    "audio/": AttachmentType.AUDIO,
    "video/": AttachmentType.VIDEO,
}

# Audio MIME types that should be marked as "voice" (common iMessage voice formats)
_VOICE_MIME_TYPES = frozenset({"audio/x-caf", "audio/caf", "audio/amr"})

# Core Data epoch: 2001-01-01 00:00:00 UTC in Unix time
_CORE_DATA_EPOCH = 978307200
# chat.db stores timestamps in nanoseconds since Core Data epoch
_NS_FACTOR = 1_000_000_000


class IMessageChannel(Channel):
    """iMessage channel that polls chat.db for incoming messages."""

    MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"

    def __init__(
        self,
        allowed_senders: list[str],
        poll_interval: int = 3,
    ):
        self._allowed_senders = set(allowed_senders)
        self._poll_interval = poll_interval

    def listen(self, callback: Callable[[str, str], str]) -> None:
        """Poll chat.db for new incoming messages and respond."""
        if sys.platform != "darwin":
            raise RuntimeError("iMessage channel is only available on macOS")

        if not self.MESSAGES_DB.exists():
            raise RuntimeError(
                f"Messages database not found: {self.MESSAGES_DB}. "
                "Ensure Full Disk Access is granted."
            )

        last_rowid = self._get_latest_rowid()
        logger.info(
            "iMessage listener started (watching for: %s, last_rowid=%d)",
            ", ".join(self._allowed_senders),
            last_rowid,
        )

        consecutive_errors = 0
        max_backoff = 60  # seconds

        while not self._stop_requested:
            try:
                new_messages = self._poll(last_rowid)
                consecutive_errors = 0  # reset on success
                for msg in new_messages:
                    # Always advance rowid so we never reprocess a message
                    last_rowid = max(last_rowid, msg["rowid"])
                    sender = msg["sender"]
                    text = msg["text"]
                    attachments: list[Attachment] = msg.get("attachments", [])
                    # Skip our own replies (iMessage to self shows as is_from_me=0)
                    if text and text.startswith(MESSAGE_PREFIX):
                        logger.debug("Skipping own message: %s", text[:40])
                        continue
                    if sender in self._allowed_senders:
                        logger.info("Message from %s: %s", sender, (text or "")[:80])
                        try:
                            if attachments:
                                # Build a full IncomingMessage with attachments
                                incoming = IncomingMessage(
                                    sender_id=sender,
                                    text=text or None,
                                    attachments=attachments,
                                    channel="imessage",
                                )
                                response = callback(incoming)  # type: ignore[call-arg,arg-type]
                            else:
                                response = callback(sender, text)
                            self.send(sender, response)
                        except Exception:
                            logger.exception("Error handling message from %s", sender)
            except Exception:
                consecutive_errors += 1
                backoff = min(self._poll_interval * (2**consecutive_errors), max_backoff)
                logger.exception(
                    "Error polling messages (consecutive=%d, backoff=%.1fs)",
                    consecutive_errors,
                    backoff,
                )
                time.sleep(backoff)
                continue

            time.sleep(self._poll_interval)

        logger.info("iMessage listener stopped")

    def send(self, recipient: str, text: str | None) -> None:
        """Send an iMessage via AppleScript."""
        if not text:
            logger.debug("Skipping empty message to %s", recipient)
            return
        prefixed = f"{MESSAGE_PREFIX} {text}"

        # Escape for AppleScript
        escaped = prefixed.replace("\\", "\\\\").replace('"', '\\"')

        applescript = f"""
        tell application "Messages"
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to participant "{recipient}" of targetService
            send "{escaped}" to targetBuddy
        end tell
        """

        try:
            subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            logger.info("Sent reply to %s (%d chars)", recipient, len(text))
        except subprocess.CalledProcessError as e:
            logger.error("Failed to send iMessage: %s", e.stderr)
            raise

    def wait_for_reply(self, sender_id: str, timeout_seconds: int = 60) -> str | None:
        """Wait for a reply from a specific sender within a timeout.

        Returns the reply text, or None if timeout is reached.
        """
        start_rowid = self._get_latest_rowid()
        deadline = time.time() + timeout_seconds
        logger.info("Waiting for reply from %s (timeout=%ds)", sender_id, timeout_seconds)

        while time.time() < deadline:
            time.sleep(self._poll_interval)
            try:
                conn = sqlite3.connect(f"file:{self.MESSAGES_DB}?mode=ro", uri=True)
                try:
                    cursor = conn.execute(
                        """
                        SELECT m.ROWID, m.text
                        FROM message m
                        LEFT JOIN handle h ON m.handle_id = h.ROWID
                        WHERE m.ROWID > ?
                          AND m.is_from_me = 0
                          AND m.text IS NOT NULL
                          AND m.text != ''
                          AND h.id = ?
                        ORDER BY m.ROWID ASC
                        LIMIT 1
                        """,
                        (start_rowid, sender_id),
                    )
                    row = cursor.fetchone()
                    if row:
                        logger.info("Got reply from %s: %s", sender_id, row[1][:40])
                        return row[1]
                finally:
                    conn.close()
            except Exception:
                logger.exception("Error polling for reply")

        logger.info("Timeout waiting for reply from %s", sender_id)
        return None

    def _get_latest_rowid(self) -> int:
        """Get the highest ROWID in chat.db."""
        conn = sqlite3.connect(f"file:{self.MESSAGES_DB}?mode=ro", uri=True)
        try:
            cursor = conn.execute("SELECT MAX(ROWID) FROM message")
            row = cursor.fetchone()
            return row[0] or 0
        finally:
            conn.close()

    def _poll(self, after_rowid: int) -> list[dict]:
        """Fetch messages newer than after_rowid, including those with attachments."""
        conn = sqlite3.connect(f"file:{self.MESSAGES_DB}?mode=ro", uri=True)
        try:
            # Query messages that have text OR attachments (via the join table).
            # The LEFT JOIN to message_attachment_join lets us detect messages
            # that have attachments without filtering them out.
            cursor = conn.execute(
                """
                SELECT DISTINCT
                    m.ROWID,
                    m.text,
                    h.id as sender,
                    m.is_from_me,
                    m.date
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                LEFT JOIN message_attachment_join maj ON m.ROWID = maj.message_id
                WHERE m.ROWID > ?
                  AND m.is_from_me = 0
                  AND (
                      (m.text IS NOT NULL AND m.text != '')
                      OR maj.attachment_id IS NOT NULL
                  )
                ORDER BY m.ROWID ASC
                """,
                (after_rowid,),
            )

            messages = []
            for row in cursor.fetchall():
                msg_rowid = row[0]
                attachments = self._query_attachments(conn, msg_rowid)
                messages.append(
                    {
                        "rowid": msg_rowid,
                        "text": row[1] or "",
                        "sender": row[2],
                        "is_from_me": row[3],
                        "date": row[4],
                        "attachments": attachments,
                    }
                )
            return messages
        finally:
            conn.close()

    @staticmethod
    def _query_attachments(conn: sqlite3.Connection, message_rowid: int) -> list[Attachment]:
        """Query attachments for a specific message from chat.db."""
        cursor = conn.execute(
            """
            SELECT
                a.filename,
                a.mime_type,
                a.transfer_name,
                a.total_bytes
            FROM attachment a
            JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
            WHERE maj.message_id = ?
            """,
            (message_rowid,),
        )

        attachments: list[Attachment] = []
        for row in cursor.fetchall():
            raw_filename: str | None = row[0]
            mime_type: str | None = row[1]
            transfer_name: str | None = row[2]
            total_bytes: int | None = row[3]

            # Resolve the file path (chat.db stores paths with ~ prefix)
            file_path: Path | None = None
            if raw_filename:
                expanded = Path(raw_filename).expanduser()
                if expanded.exists():
                    file_path = expanded
                else:
                    logger.warning("iMessage attachment file missing from disk: %s", expanded)

            # Determine attachment type from MIME type
            attachment_type = AttachmentType.FILE
            if mime_type:
                if mime_type in _VOICE_MIME_TYPES:
                    attachment_type = AttachmentType.VOICE
                else:
                    for prefix, atype in _MIME_TO_ATTACHMENT_TYPE.items():
                        if mime_type.startswith(prefix):
                            attachment_type = atype
                            break

            # Skip attachments with no file on disk and no useful metadata
            if file_path is None and not transfer_name:
                continue

            attachments.append(
                Attachment(
                    type=attachment_type,
                    file_path=file_path,
                    mime_type=mime_type,
                    file_name=transfer_name,
                    file_size=total_bytes,
                )
            )

        return attachments


def register_plugin() -> tuple[ChannelPluginMeta, Callable[[dict[str, Any]], Channel]]:
    """Return plugin metadata and factory for the iMessage channel."""
    from creel.channels.plugin import ChannelCapability, ChannelPluginMeta
    from creel.models import IMessageChannelConfig

    meta = ChannelPluginMeta(
        id="imessage",
        label="iMessage",
        capabilities=(
            ChannelCapability.POLLING | ChannelCapability.SEND | ChannelCapability.WAIT_FOR_REPLY
        ),
        config_schema=IMessageChannelConfig,
        platform="darwin",
    )

    def factory(config: dict[str, Any]) -> IMessageChannel:
        cfg = IMessageChannelConfig(**config)
        return IMessageChannel(
            allowed_senders=[cfg.listen_to],
            poll_interval=cfg.poll_interval,
        )

    return meta, factory
