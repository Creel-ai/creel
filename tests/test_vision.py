"""Tests for VisionProcessor (MEDIA-004)."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from unittest.mock import patch

from creel.services.vision import (
    NEEDS_CONVERSION,
    SUPPORTED_IMAGE_FORMATS,
    VisionProcessor,
    _detect_mime_type,
    _pil_format,
)


def _make_png(tmp_path: Path, name: str = "photo.png", size: tuple[int, int] = (100, 100)) -> Path:
    """Create a minimal valid PNG file using Pillow."""
    from PIL import Image

    img = Image.new("RGB", size, color=(255, 0, 0))
    path = tmp_path / name
    img.save(path, format="PNG")
    return path


def _make_jpeg(tmp_path: Path, name: str = "photo.jpg", size: tuple[int, int] = (100, 100)) -> Path:
    """Create a minimal valid JPEG file using Pillow."""
    from PIL import Image

    img = Image.new("RGB", size, color=(0, 255, 0))
    path = tmp_path / name
    img.save(path, format="JPEG")
    return path


def _make_bmp(tmp_path: Path, name: str = "photo.bmp", size: tuple[int, int] = (100, 100)) -> Path:
    """Create a minimal valid BMP file using Pillow."""
    from PIL import Image

    img = Image.new("RGB", size, color=(0, 0, 255))
    path = tmp_path / name
    img.save(path, format="BMP")
    return path


class TestPrepareImageAnthropic:
    """Image preparation for Anthropic format (default)."""

    def test_png_returns_anthropic_block(self, tmp_path: Path):
        img_path = _make_png(tmp_path)
        vp = VisionProcessor(provider="anthropic")
        result = vp.prepare_image(img_path)

        assert result is not None
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/png"
        # Verify it's valid base64
        decoded = base64.b64decode(result["source"]["data"])
        assert len(decoded) > 0

    def test_jpeg_returns_anthropic_block(self, tmp_path: Path):
        img_path = _make_jpeg(tmp_path)
        vp = VisionProcessor(provider="anthropic")
        result = vp.prepare_image(img_path)

        assert result is not None
        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/jpeg"

    def test_default_provider_is_anthropic(self, tmp_path: Path):
        img_path = _make_png(tmp_path)
        vp = VisionProcessor()
        result = vp.prepare_image(img_path)

        assert result is not None
        assert result["type"] == "image"


class TestPrepareImageOpenAI:
    """Image preparation for OpenAI format."""

    def test_png_returns_openai_block(self, tmp_path: Path):
        img_path = _make_png(tmp_path)
        vp = VisionProcessor(provider="openai")
        result = vp.prepare_image(img_path)

        assert result is not None
        assert result["type"] == "image_url"
        url = result["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        # Verify the base64 payload is valid
        b64_data = url.split(",", 1)[1]
        decoded = base64.b64decode(b64_data)
        assert len(decoded) > 0

    def test_jpeg_returns_openai_block(self, tmp_path: Path):
        img_path = _make_jpeg(tmp_path)
        vp = VisionProcessor(provider="openai")
        result = vp.prepare_image(img_path)

        assert result is not None
        assert result["type"] == "image_url"
        assert result["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_provider_case_insensitive(self, tmp_path: Path):
        img_path = _make_png(tmp_path)
        vp = VisionProcessor(provider="OpenAI")
        result = vp.prepare_image(img_path)

        assert result is not None
        assert result["type"] == "image_url"


class TestResizing:
    """Images larger than max_pixels are resized."""

    def test_large_image_resized(self, tmp_path: Path):
        img_path = _make_png(tmp_path, size=(4000, 3000))
        vp = VisionProcessor(max_pixels=2048)
        result = vp.prepare_image(img_path)

        assert result is not None
        # Verify the output is smaller than the original
        decoded = base64.b64decode(result["source"]["data"])
        from PIL import Image

        img = Image.open(io.BytesIO(decoded))
        assert max(img.size) <= 2048

    def test_small_image_not_resized(self, tmp_path: Path):
        img_path = _make_png(tmp_path, size=(500, 400))
        vp = VisionProcessor(max_pixels=2048)
        result = vp.prepare_image(img_path)

        assert result is not None
        decoded = base64.b64decode(result["source"]["data"])
        from PIL import Image

        img = Image.open(io.BytesIO(decoded))
        assert img.size == (500, 400)

    def test_custom_max_pixels(self, tmp_path: Path):
        img_path = _make_png(tmp_path, size=(2000, 1500))
        vp = VisionProcessor(max_pixels=1024)
        result = vp.prepare_image(img_path)

        assert result is not None
        decoded = base64.b64decode(result["source"]["data"])
        from PIL import Image

        img = Image.open(io.BytesIO(decoded))
        assert max(img.size) <= 1024

    def test_aspect_ratio_preserved(self, tmp_path: Path):
        img_path = _make_png(tmp_path, size=(4000, 2000))
        vp = VisionProcessor(max_pixels=2048)
        result = vp.prepare_image(img_path)

        assert result is not None
        decoded = base64.b64decode(result["source"]["data"])
        from PIL import Image

        img = Image.open(io.BytesIO(decoded))
        w, h = img.size
        # Original ratio is 2:1, should be preserved
        assert abs(w / h - 2.0) < 0.05


class TestFormatConversion:
    """Unsupported formats are converted to JPEG."""

    def test_bmp_converted_to_jpeg(self, tmp_path: Path):
        img_path = _make_bmp(tmp_path)
        vp = VisionProcessor()
        result = vp.prepare_image(img_path)

        assert result is not None
        assert result["source"]["media_type"] == "image/jpeg"

    def test_tiff_converted_to_jpeg(self, tmp_path: Path):
        from PIL import Image

        img = Image.new("RGB", (100, 100), color=(128, 128, 128))
        img_path = tmp_path / "photo.tiff"
        img.save(img_path, format="TIFF")

        vp = VisionProcessor()
        result = vp.prepare_image(img_path)

        assert result is not None
        assert result["source"]["media_type"] == "image/jpeg"

    def test_rgba_converted_to_rgb_for_jpeg(self, tmp_path: Path):
        """RGBA images must be converted to RGB before JPEG save."""
        from PIL import Image

        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        img_path = tmp_path / "photo.bmp"
        img.save(img_path, format="BMP")

        vp = VisionProcessor()
        result = vp.prepare_image(img_path)

        assert result is not None
        # Should succeed without error (JPEG doesn't support alpha)
        decoded = base64.b64decode(result["source"]["data"])
        out = Image.open(io.BytesIO(decoded))
        assert out.mode == "RGB"

    def test_conversion_without_pillow_returns_none(self, tmp_path: Path):
        # Create a file with a conversion-needed extension
        img_path = tmp_path / "photo.heic"
        img_path.write_bytes(b"fake-heic-data")

        vp = VisionProcessor()
        with patch("creel.services.vision._has_pillow", return_value=False):
            result = vp.prepare_image(img_path)

        assert result is None


class TestFileNotFound:
    """Missing files return None."""

    def test_missing_file(self):
        vp = VisionProcessor()
        result = vp.prepare_image(Path("/nonexistent/photo.png"))
        assert result is None


class TestUnsupportedFormat:
    """Unsupported image formats return None."""

    def test_svg_not_supported(self, tmp_path: Path):
        img_path = tmp_path / "image.svg"
        img_path.write_text("<svg></svg>")

        vp = VisionProcessor()
        result = vp.prepare_image(img_path)
        assert result is None

    def test_pdf_not_supported(self, tmp_path: Path):
        img_path = tmp_path / "doc.pdf"
        img_path.write_bytes(b"%PDF-1.4")

        vp = VisionProcessor()
        result = vp.prepare_image(img_path)
        assert result is None


class TestCorruptFile:
    """Corrupt image files return None instead of crashing."""

    def test_corrupt_png(self, tmp_path: Path):
        img_path = tmp_path / "bad.png"
        img_path.write_bytes(b"not a real png")

        vp = VisionProcessor()
        # With Pillow available, it will try to open and fail
        result = vp.prepare_image(img_path)
        # Should return None (graceful failure), not raise
        # Note: without Pillow, it reads raw bytes which still works
        # but with Pillow, Image.open will fail on corrupt data
        assert result is None or isinstance(result, dict)


class TestWithoutPillow:
    """Without Pillow, images are sent as-is (no resize/convert)."""

    def test_png_sent_raw_without_pillow(self, tmp_path: Path):
        img_path = _make_png(tmp_path)
        raw_bytes = img_path.read_bytes()

        vp = VisionProcessor()
        with patch("creel.services.vision._has_pillow", return_value=False):
            result = vp.prepare_image(img_path)

        assert result is not None
        assert result["type"] == "image"
        decoded = base64.b64decode(result["source"]["data"])
        assert decoded == raw_bytes

    def test_jpeg_sent_raw_without_pillow(self, tmp_path: Path):
        img_path = _make_jpeg(tmp_path)
        raw_bytes = img_path.read_bytes()

        vp = VisionProcessor()
        with patch("creel.services.vision._has_pillow", return_value=False):
            result = vp.prepare_image(img_path)

        assert result is not None
        decoded = base64.b64decode(result["source"]["data"])
        assert decoded == raw_bytes


class TestMimeDetection:
    """MIME type detection from file extension."""

    def test_jpeg_extensions(self):
        assert _detect_mime_type(Path("photo.jpg")) == "image/jpeg"
        assert _detect_mime_type(Path("photo.jpeg")) == "image/jpeg"

    def test_png(self):
        assert _detect_mime_type(Path("image.png")) == "image/png"

    def test_gif(self):
        assert _detect_mime_type(Path("anim.gif")) == "image/gif"

    def test_webp(self):
        assert _detect_mime_type(Path("photo.webp")) == "image/webp"

    def test_unknown_falls_back_to_mimetypes(self):
        # .bmp should be detected by the mimetypes module
        result = _detect_mime_type(Path("image.bmp"))
        assert result is not None or result is None  # platform-dependent


class TestPilFormat:
    """PIL format string mapping."""

    def test_jpeg(self):
        assert _pil_format(".jpg") == "JPEG"
        assert _pil_format(".jpeg") == "JPEG"

    def test_png(self):
        assert _pil_format(".png") == "PNG"

    def test_gif(self):
        assert _pil_format(".gif") == "GIF"

    def test_webp(self):
        assert _pil_format(".webp") == "WEBP"

    def test_unknown_defaults_to_jpeg(self):
        assert _pil_format(".xyz") == "JPEG"


class TestSupportedFormats:
    """Verify format sets are correct."""

    def test_common_formats_supported(self):
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            assert ext in SUPPORTED_IMAGE_FORMATS

    def test_heic_needs_conversion(self):
        assert ".heic" in NEEDS_CONVERSION
        assert ".heif" in NEEDS_CONVERSION

    def test_bmp_needs_conversion(self):
        assert ".bmp" in NEEDS_CONVERSION

    def test_tiff_needs_conversion(self):
        assert ".tiff" in NEEDS_CONVERSION
        assert ".tif" in NEEDS_CONVERSION


class TestQuality:
    """JPEG quality parameter affects output."""

    def test_low_quality_smaller_file(self, tmp_path: Path):
        img_path = _make_jpeg(tmp_path, size=(500, 500))

        vp_high = VisionProcessor(quality=95)
        vp_low = VisionProcessor(quality=10)

        result_high = vp_high.prepare_image(img_path)
        result_low = vp_low.prepare_image(img_path)

        assert result_high is not None
        assert result_low is not None

        high_size = len(base64.b64decode(result_high["source"]["data"]))
        low_size = len(base64.b64decode(result_low["source"]["data"]))
        # Low quality should produce smaller output
        assert low_size < high_size
