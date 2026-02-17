"""iMessage channel - polls chat.db for incoming messages, replies via AppleScript."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from taskrunner.channels import Channel
from taskrunner.outputs import MESSAGE_PREFIX

logger = logging.getLogger(__name__)

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
                    # Skip our own replies (iMessage to self shows as is_from_me=0)
                    if text.startswith(MESSAGE_PREFIX):
                        logger.debug("Skipping own message: %s", text[:40])
                        continue
                    if sender in self._allowed_senders:
                        logger.info("Message from %s: %s", sender, text[:80])
                        try:
                            response = callback(sender, text)
                            self.send(sender, response)
                        except Exception:
                            logger.exception("Error handling message from %s", sender)
            except Exception:
                consecutive_errors += 1
                backoff = min(self._poll_interval * (2 ** consecutive_errors), max_backoff)
                logger.exception(
                    "Error polling messages (consecutive=%d, backoff=%.1fs)",
                    consecutive_errors, backoff,
                )
                time.sleep(backoff)
                continue

            time.sleep(self._poll_interval)

        logger.info("iMessage listener stopped")

    def send(self, recipient: str, text: str) -> None:
        """Send an iMessage via AppleScript."""
        prefixed = f"{MESSAGE_PREFIX} {text}"

        # Escape for AppleScript
        escaped = prefixed.replace("\\", "\\\\").replace('"', '\\"')

        applescript = f'''
        tell application "Messages"
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to participant "{recipient}" of targetService
            send "{escaped}" to targetBuddy
        end tell
        '''

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
        """Fetch messages newer than after_rowid."""
        conn = sqlite3.connect(f"file:{self.MESSAGES_DB}?mode=ro", uri=True)
        try:
            cursor = conn.execute(
                """
                SELECT
                    m.ROWID,
                    m.text,
                    h.id as sender,
                    m.is_from_me,
                    m.date
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.ROWID > ?
                  AND m.is_from_me = 0
                  AND m.text IS NOT NULL
                  AND m.text != ''
                ORDER BY m.ROWID ASC
                """,
                (after_rowid,),
            )

            messages = []
            for row in cursor.fetchall():
                messages.append({
                    "rowid": row[0],
                    "text": row[1],
                    "sender": row[2],
                    "is_from_me": row[3],
                    "date": row[4],
                })
            return messages
        finally:
            conn.close()


def register_plugin() -> tuple[ChannelPluginMeta, Callable[[dict[str, Any]], Channel]]:
    """Return plugin metadata and factory for the iMessage channel."""
    from taskrunner.channels.plugin import ChannelCapability, ChannelPluginMeta
    from taskrunner.models import IMessageChannelConfig

    meta = ChannelPluginMeta(
        id="imessage",
        label="iMessage",
        capabilities=(
            ChannelCapability.POLLING
            | ChannelCapability.SEND
            | ChannelCapability.WAIT_FOR_REPLY
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
