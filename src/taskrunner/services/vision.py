"""Image processing for LLM vision models.

Converts images to the content-block format required by the configured LLM
provider (Anthropic or OpenAI-compatible).
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

logger = logging.getLogger(__name__)

# Formats natively supported by major vision APIs
SUPPORTED_IMAGE_FORMATS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
    }
)

# Formats that need conversion to JPEG before sending
NEEDS_CONVERSION = frozenset({".heic", ".heif", ".tiff", ".tif", ".bmp"})

# MIME types for supported formats
_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Default configuration
_DEFAULT_MAX_PIXELS = 2048
_DEFAULT_QUALITY = 85


def _has_pillow() -> bool:
    """Check if Pillow is available."""
    try:
        import PIL  # noqa: F401

        return True
    except ImportError:
        return False


def _detect_mime_type(file_path: Path) -> str | None:
    """Detect MIME type from extension or mimetypes module."""
    ext = file_path.suffix.lower()
    if ext in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext]
    mime, _ = mimetypes.guess_type(str(file_path))
    return mime


class VisionProcessor:
    """Process images for LLM vision models.

    Converts images to base64-encoded content blocks in either Anthropic
    or OpenAI format, with optional resizing and format conversion.

    Parameters
    ----------
    provider : str
        LLM provider format: ``"anthropic"`` or ``"openai"``.
    max_pixels : int
        Maximum dimension (width or height) in pixels. Images larger than
        this are resized while preserving aspect ratio.
    quality : int
        JPEG compression quality (1-100) used when resizing or converting.
    """

    def __init__(
        self,
        provider: str = "anthropic",
        max_pixels: int = _DEFAULT_MAX_PIXELS,
        quality: int = _DEFAULT_QUALITY,
    ) -> None:
        self._provider = provider.lower()
        self._max_pixels = max_pixels
        self._quality = quality

    def prepare_image(self, file_path: Path) -> dict | None:
        """Convert an image file to an LLM content block.

        Returns a dict suitable for inclusion in the message ``content``
        list, or *None* if the file cannot be processed.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            logger.warning("Image file not found: %s", file_path)
            return None

        ext = file_path.suffix.lower()

        # Determine if we need Pillow for conversion or resizing
        needs_pillow = ext in NEEDS_CONVERSION

        if needs_pillow and not _has_pillow():
            logger.warning(
                "Pillow is required to process %s images. "
                "Install with: pip install Pillow",
                ext,
            )
            return None

        if ext not in SUPPORTED_IMAGE_FORMATS and ext not in NEEDS_CONVERSION:
            logger.warning("Unsupported image format: %s", ext)
            return None

        try:
            image_bytes, media_type = self._load_and_process(file_path)
        except Exception:
            logger.warning("Failed to process image %s", file_path, exc_info=True)
            return None

        encoded = base64.b64encode(image_bytes).decode("ascii")
        logger.info(
            "Prepared image %s (%s, %d bytes encoded)",
            file_path.name,
            media_type,
            len(image_bytes),
        )

        if self._provider == "openai":
            return {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{media_type};base64,{encoded}",
                },
            }

        # Anthropic format (default)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": encoded,
            },
        }

    def _load_and_process(self, file_path: Path) -> tuple[bytes, str]:
        """Load, optionally resize/convert, and return (bytes, mime_type)."""
        ext = file_path.suffix.lower()

        if ext in NEEDS_CONVERSION:
            return self._convert_with_pillow(file_path)

        if ext in SUPPORTED_IMAGE_FORMATS and _has_pillow():
            return self._maybe_resize(file_path)

        # No Pillow — send as-is
        data = file_path.read_bytes()
        media_type = _detect_mime_type(file_path) or "image/jpeg"
        return data, media_type

    def _maybe_resize(self, file_path: Path) -> tuple[bytes, str]:
        """Resize the image if it exceeds max_pixels, preserving format."""
        import io

        from PIL import Image

        img = Image.open(file_path)
        media_type = _detect_mime_type(file_path) or "image/jpeg"

        w, h = img.size
        if max(w, h) > self._max_pixels:
            ratio = self._max_pixels / max(w, h)
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.debug(
                "Resized %s from %dx%d to %dx%d", file_path.name, w, h, new_w, new_h
            )

        buf = io.BytesIO()
        # Determine PIL save format from extension
        pil_format = _pil_format(file_path.suffix.lower())
        save_kwargs: dict = {}
        if pil_format == "JPEG":
            # Convert RGBA to RGB for JPEG
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            save_kwargs["quality"] = self._quality
        img.save(buf, format=pil_format, **save_kwargs)
        return buf.getvalue(), media_type

    def _convert_with_pillow(self, file_path: Path) -> tuple[bytes, str]:
        """Convert unsupported formats (HEIC, BMP, TIFF) to JPEG."""
        import io

        from PIL import Image

        img = Image.open(file_path)

        w, h = img.size
        if max(w, h) > self._max_pixels:
            ratio = self._max_pixels / max(w, h)
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.debug(
                "Resized %s from %dx%d to %dx%d",
                file_path.name,
                w,
                h,
                new_w,
                new_h,
            )

        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._quality)
        logger.debug("Converted %s to JPEG", file_path.name)
        return buf.getvalue(), "image/jpeg"


def _pil_format(ext: str) -> str:
    """Map file extension to PIL save format string."""
    return {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
        ".gif": "GIF",
        ".webp": "WEBP",
    }.get(ext, "JPEG")
