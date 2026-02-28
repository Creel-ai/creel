"""Tests for MediaStore service (MEDIA-002)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.channels.message import Attachment, AttachmentType
from taskrunner.services.media_store import MediaStore


class TestSaveFromBytes:
    """Save media from in-memory bytes (attachment.data)."""

    def test_save_image_bytes(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.IMAGE,
            data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
            mime_type="image/png",
        )
        path = store.save_media(att, channel="telegram")
        assert path.exists()
        assert path.suffix == ".png"
        assert path.read_bytes() == att.data
        # Organized under channel/date directory
        assert "telegram" in str(path)

    def test_save_voice_bytes(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.VOICE,
            data=b"OggS" + b"\x00" * 50,
            mime_type="audio/ogg",
        )
        path = store.save_media(att, channel="telegram")
        assert path.exists()
        assert path.suffix == ".ogg"

    def test_directory_structure(self, tmp_path: Path):
        """Files are organized as base_dir/channel/YYYY-MM-DD/uuid.ext."""
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.IMAGE, data=b"fake-jpg", mime_type="image/jpeg"
        )
        path = store.save_media(att, channel="imessage")
        parts = path.relative_to(tmp_path / "media").parts
        assert len(parts) == 3  # channel / date / file
        assert parts[0] == "imessage"
        # Date part should be YYYY-MM-DD format
        assert len(parts[1]) == 10 and parts[1].count("-") == 2

    def test_default_channel(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(type=AttachmentType.IMAGE, data=b"data")
        path = store.save_media(att)
        assert "unknown" in str(path)

    def test_max_file_size_exceeded(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media", max_file_size=100)
        att = Attachment(type=AttachmentType.IMAGE, data=b"x" * 200)
        with pytest.raises(ValueError, match="exceeds max file size"):
            store.save_media(att)


class TestSaveFromFilePath:
    """Save media by copying from an existing file."""

    def test_save_from_file(self, tmp_path: Path):
        source = tmp_path / "source.jpg"
        source.write_bytes(b"jpeg-data-here")

        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.IMAGE,
            file_path=source,
            mime_type="image/jpeg",
        )
        path = store.save_media(att, channel="imessage")
        assert path.exists()
        assert path.read_bytes() == b"jpeg-data-here"
        assert path != source  # saved to a new location

    def test_file_not_found(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.IMAGE,
            file_path=Path("/nonexistent/file.jpg"),
        )
        with pytest.raises(FileNotFoundError):
            store.save_media(att)

    def test_file_exceeds_max_size(self, tmp_path: Path):
        source = tmp_path / "big.jpg"
        source.write_bytes(b"x" * 200)
        store = MediaStore(base_dir=tmp_path / "media", max_file_size=100)
        att = Attachment(type=AttachmentType.IMAGE, file_path=source)
        with pytest.raises(ValueError, match="exceeds max file size"):
            store.save_media(att)

    def test_extension_from_filename(self, tmp_path: Path):
        source = tmp_path / "recording.m4a"
        source.write_bytes(b"audio-data")
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.VOICE,
            file_path=source,
            file_name="recording.m4a",
        )
        path = store.save_media(att, channel="telegram")
        assert path.suffix == ".m4a"


class TestSaveFromURL:
    """Save media by downloading from a URL."""

    def test_download_and_save(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.IMAGE,
            url="https://example.com/photo.jpg",
            mime_type="image/jpeg",
        )

        fake_response = MagicMock()
        fake_response.headers = {"content-length": "100"}
        fake_response.iter_bytes = MagicMock(return_value=[b"jpeg-bytes-here"])
        fake_response.raise_for_status = MagicMock()
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("httpx.stream", return_value=fake_response):
            path = store.save_media(att, channel="telegram")

        assert path.exists()
        assert path.read_bytes() == b"jpeg-bytes-here"
        assert path.suffix == ".jpg"

    def test_download_exceeds_content_length(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media", max_file_size=100)
        att = Attachment(
            type=AttachmentType.IMAGE,
            url="https://example.com/huge.jpg",
        )

        fake_response = MagicMock()
        fake_response.headers = {"content-length": "200"}
        fake_response.raise_for_status = MagicMock()
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("httpx.stream", return_value=fake_response):
            with pytest.raises(ValueError, match="exceeds max size"):
                store.save_media(att)

    def test_download_exceeds_size_during_streaming(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media", max_file_size=100)
        att = Attachment(type=AttachmentType.IMAGE, url="https://example.com/big.jpg")

        fake_response = MagicMock()
        fake_response.headers = {}  # No content-length header
        fake_response.iter_bytes = MagicMock(return_value=[b"x" * 200])
        fake_response.raise_for_status = MagicMock()
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch("httpx.stream", return_value=fake_response):
            with pytest.raises(ValueError, match="exceeded max file size"):
                store.save_media(att)


class TestNoSourceError:
    """Attachment with no data, file_path, or url raises ValueError."""

    def test_no_source(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(type=AttachmentType.IMAGE)
        with pytest.raises(ValueError, match="no data, file_path, or url"):
            store.save_media(att)


class TestDeduplication:
    """Identical files (by content hash) return the existing path."""

    def test_same_content_returns_existing_path(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        data = b"identical-content-bytes"
        att1 = Attachment(type=AttachmentType.IMAGE, data=data, mime_type="image/jpeg")
        att2 = Attachment(type=AttachmentType.IMAGE, data=data, mime_type="image/jpeg")

        path1 = store.save_media(att1, channel="telegram")
        path2 = store.save_media(att2, channel="telegram")
        assert path1 == path2

    def test_different_content_saves_separately(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att1 = Attachment(
            type=AttachmentType.IMAGE, data=b"content-a", mime_type="image/jpeg"
        )
        att2 = Attachment(
            type=AttachmentType.IMAGE, data=b"content-b", mime_type="image/jpeg"
        )

        path1 = store.save_media(att1, channel="telegram")
        path2 = store.save_media(att2, channel="telegram")
        assert path1 != path2

    def test_dedup_falls_back_if_file_deleted(self, tmp_path: Path):
        """If the cached file was deleted, save again."""
        store = MediaStore(base_dir=tmp_path / "media")
        data = b"will-be-deleted"
        att = Attachment(type=AttachmentType.IMAGE, data=data, mime_type="image/png")

        path1 = store.save_media(att, channel="c")
        path1.unlink()  # remove the file

        path2 = store.save_media(att, channel="c")
        assert path2 != path1
        assert path2.exists()


class TestCleanup:
    """Remove files older than retention period."""

    def test_cleanup_old_files(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media", retention_days=7)
        # Create a file and backdate it
        old_dir = tmp_path / "media" / "telegram" / "2020-01-01"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "old.jpg"
        old_file.write_bytes(b"old")
        # Set mtime to 30 days ago
        old_time = time.time() - (30 * 86400)
        import os

        os.utime(old_file, (old_time, old_time))

        deleted = store.cleanup()
        assert deleted == 1
        assert not old_file.exists()

    def test_cleanup_preserves_recent_files(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media", retention_days=7)
        recent_dir = tmp_path / "media" / "telegram" / "2026-02-26"
        recent_dir.mkdir(parents=True)
        recent_file = recent_dir / "recent.jpg"
        recent_file.write_bytes(b"recent")

        deleted = store.cleanup()
        assert deleted == 0
        assert recent_file.exists()

    def test_cleanup_custom_retention(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media", retention_days=30)
        old_dir = tmp_path / "media" / "c" / "d"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "f.jpg"
        old_file.write_bytes(b"data")
        old_time = time.time() - (2 * 86400)
        import os

        os.utime(old_file, (old_time, old_time))

        # Default 30 days — file is only 2 days old, should be preserved
        assert store.cleanup() == 0
        # Override to 1 day — file should be deleted
        assert store.cleanup(retention_days=1) == 1

    def test_cleanup_prunes_empty_dirs(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        empty_dir = tmp_path / "media" / "telegram" / "2020-01-01"
        empty_dir.mkdir(parents=True)
        store.cleanup()
        assert not empty_dir.exists()

    def test_cleanup_nonexistent_base_dir(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "nonexistent")
        assert store.cleanup() == 0


class TestExtensionResolution:
    """File extension is resolved from file_name, mime_type, or type fallback."""

    def test_from_file_name(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.IMAGE,
            data=b"data",
            file_name="photo.heic",
        )
        path = store.save_media(att, channel="c")
        assert path.suffix == ".heic"

    def test_from_mime_type(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(
            type=AttachmentType.IMAGE,
            data=b"data",
            mime_type="image/webp",
        )
        path = store.save_media(att, channel="c")
        assert path.suffix == ".webp"

    def test_fallback_to_type(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(type=AttachmentType.VOICE, data=b"data")
        path = store.save_media(att, channel="c")
        assert path.suffix == ".ogg"

    def test_file_type_fallback(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        att = Attachment(type=AttachmentType.FILE, data=b"data")
        path = store.save_media(att, channel="c")
        assert path.suffix == ".bin"


class TestBaseDirProperty:
    def test_base_dir(self, tmp_path: Path):
        store = MediaStore(base_dir=tmp_path / "media")
        assert store.base_dir == tmp_path / "media"


class TestBaseDirCreatedOnFirstUse:
    def test_dirs_created_on_save(self, tmp_path: Path):
        media_dir = tmp_path / "new" / "media"
        assert not media_dir.exists()
        store = MediaStore(base_dir=media_dir)
        att = Attachment(
            type=AttachmentType.IMAGE, data=b"data", mime_type="image/jpeg"
        )
        store.save_media(att, channel="c")
        assert media_dir.exists()
