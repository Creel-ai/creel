"""iMessage channel - polls chat.db for incoming messages, replies via AppleScript."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

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

        while True:
            try:
                new_messages = self._poll(last_rowid)
                for msg in new_messages:
                    sender = msg["sender"]
                    if sender in self._allowed_senders:
                        logger.info("Message from %s: %s", sender, msg["text"][:80])
                        response = callback(sender, msg["text"])
                        self.send(sender, response)
                    last_rowid = max(last_rowid, msg["rowid"])
            except Exception:
                logger.exception("Error polling messages")

            time.sleep(self._poll_interval)

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
