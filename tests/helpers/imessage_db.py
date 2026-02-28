"""Shared helpers for building minimal iMessage chat.db test databases."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def create_chat_db(db_path: Path) -> None:
    """Create a minimal chat.db schema at *db_path*."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY,
            id TEXT
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            handle_id INTEGER,
            is_from_me INTEGER DEFAULT 0,
            date INTEGER DEFAULT 0
        );
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            filename TEXT,
            mime_type TEXT,
            transfer_name TEXT,
            total_bytes INTEGER
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER,
            attachment_id INTEGER
        );
        """
    )
    conn.close()


def insert_handle(db_path: Path, rowid: int, sender_id: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (?, ?)", (rowid, sender_id))
    conn.commit()
    conn.close()


def insert_message(
    db_path: Path,
    rowid: int,
    text: str | None,
    handle_id: int,
    is_from_me: int = 0,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO message (ROWID, text, handle_id, is_from_me, date) VALUES (?, ?, ?, ?, 0)",
        (rowid, text, handle_id, is_from_me),
    )
    conn.commit()
    conn.close()


def insert_attachment(
    db_path: Path,
    rowid: int,
    filename: str | None,
    mime_type: str | None,
    transfer_name: str | None,
    total_bytes: int | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO attachment (ROWID, filename, mime_type, transfer_name, total_bytes) "
        "VALUES (?, ?, ?, ?, ?)",
        (rowid, filename, mime_type, transfer_name, total_bytes),
    )
    conn.commit()
    conn.close()


def link_attachment(db_path: Path, message_id: int, attachment_id: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)",
        (message_id, attachment_id),
    )
    conn.commit()
    conn.close()
