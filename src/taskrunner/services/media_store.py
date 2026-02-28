"""Local filesystem media storage for incoming message attachments."""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
from pathlib import Path

import httpx

from taskrunner.channels.message import Attachment

logger = logging.getLogger(__name__)

# Default limits
DEFAULT_BASE_DIR = Path.home() / ".creel" / "media"
DEFAULT_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
DEFAULT_RETENTION_DAYS = 30
_DOWNLOAD_TIMEOUT = 30  # seconds

# Map common MIME types to file extensions
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/ogg": ".ogg",
    "audio/oga": ".oga",
    "audio/opus": ".opus",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/x-caf": ".caf",
    "video/mp4": ".mp4",
    "application/pdf": ".pdf",
}


class MediaStore:
    """Save, deduplicate, and clean up incoming media files on local disk.

    Files are organized as ``{base_dir}/{channel}/{YYYY-MM-DD}/{uuid}.{ext}``.
    """

    def __init__(
        self,
        base_dir: Path | str = DEFAULT_BASE_DIR,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._max_file_size = max_file_size
        self._retention_days = retention_days
        # In-memory content-hash → path index (populated on save)
        self._hash_index: dict[str, Path] = {}

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_media(self, attachment: Attachment, channel: str = "unknown") -> Path:
        """Persist an attachment to disk and return the saved file path.

        Supports three sources (checked in order):
        1. ``attachment.data`` — raw bytes already in memory
        2. ``attachment.file_path`` — an existing file on disk (copied)
        3. ``attachment.url`` — downloaded via httpx
        """
        data = self._resolve_bytes(attachment)
        content_hash = hashlib.sha256(data).hexdigest()

        # Deduplication: return existing file if we've already saved identical content
        if content_hash in self._hash_index:
            existing = self._hash_index[content_hash]
            if existing.exists():
                logger.debug("Deduplicated media %s -> %s", content_hash[:12], existing)
                return existing

        ext = self._resolve_extension(attachment)
        from datetime import date

        day_str = date.today().isoformat()
        dest_dir = self._base_dir / channel / day_str
        dest_dir.mkdir(parents=True, exist_ok=True)

        file_name = f"{uuid.uuid4().hex}{ext}"
        dest = dest_dir / file_name
        dest.write_bytes(data)

        self._hash_index[content_hash] = dest
        logger.info(
            "Saved media: %s (%d bytes) -> %s",
            attachment.type.value,
            len(data),
            dest,
        )
        return dest

    def cleanup(self, retention_days: int | None = None) -> int:
        """Remove files older than *retention_days*. Returns count of deleted files."""
        days = retention_days if retention_days is not None else self._retention_days
        cutoff = time.time() - (days * 86400)
        deleted = 0

        if not self._base_dir.exists():
            return 0

        for file_path in self._base_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink()
                deleted += 1
                # Remove from hash index
                self._hash_index = {
                    h: p for h, p in self._hash_index.items() if p != file_path
                }

        # Prune empty date/channel directories
        self._prune_empty_dirs()
        logger.info("Media cleanup: removed %d files older than %d days", deleted, days)
        return deleted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_bytes(self, attachment: Attachment) -> bytes:
        """Get the raw bytes for an attachment from one of its three sources."""
        if attachment.data is not None:
            if len(attachment.data) > self._max_file_size:
                raise ValueError(
                    f"Attachment data exceeds max file size "
                    f"({len(attachment.data)} > {self._max_file_size})"
                )
            return attachment.data

        if attachment.file_path is not None:
            path = Path(attachment.file_path)
            if not path.exists():
                raise FileNotFoundError(f"Attachment file not found: {path}")
            size = path.stat().st_size
            if size > self._max_file_size:
                raise ValueError(
                    f"Attachment file exceeds max file size ({size} > {self._max_file_size})"
                )
            return path.read_bytes()

        if attachment.url is not None:
            return self._download(attachment.url)

        raise ValueError("Attachment has no data, file_path, or url")

    def _download(self, url: str) -> bytes:
        """Download a file from *url* with size limit enforcement."""
        with httpx.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as resp:
            resp.raise_for_status()

            # Check Content-Length header first if available
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > self._max_file_size:
                raise ValueError(
                    f"Remote file exceeds max size "
                    f"({content_length} > {self._max_file_size})"
                )

            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > self._max_file_size:
                    raise ValueError(
                        f"Download exceeded max file size ({total} > {self._max_file_size})"
                    )
                chunks.append(chunk)

        return b"".join(chunks)

    def _resolve_extension(self, attachment: Attachment) -> str:
        """Determine the file extension from MIME type, file name, or attachment type."""
        # Try file_name first
        if attachment.file_name:
            suffix = Path(attachment.file_name).suffix
            if suffix:
                return suffix

        # Try MIME type
        if attachment.mime_type and attachment.mime_type in _MIME_TO_EXT:
            return _MIME_TO_EXT[attachment.mime_type]

        # Fallback based on attachment type
        type_defaults = {
            "image": ".jpg",
            "voice": ".ogg",
            "audio": ".mp3",
            "video": ".mp4",
            "file": ".bin",
        }
        return type_defaults.get(attachment.type.value, ".bin")

    def _prune_empty_dirs(self) -> None:
        """Remove empty directories under base_dir (bottom-up)."""
        if not self._base_dir.exists():
            return
        # Walk bottom-up so we can remove leaf dirs first
        for dirpath in sorted(self._base_dir.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                dirpath.rmdir()
