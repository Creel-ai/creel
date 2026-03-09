"""Media processing — voice transcription and image preparation for the LLM."""

from __future__ import annotations

import logging
from pathlib import Path

from creel.channels.message import Attachment, AttachmentType
from creel.models import MediaConfig
from creel.services.media_store import MediaStore
from creel.services.transcription import TranscriptionService
from creel.services.vision import VisionProcessor

logger = logging.getLogger(__name__)


class MediaProcessor:
    """Handles media attachments: saves, transcribes voice, and prepares images.

    Extracted from ChatServer to keep media concerns separate from session
    and agent orchestration.
    """

    def __init__(self, config: MediaConfig) -> None:
        storage_dir = Path(config.storage_dir).expanduser()
        self._store = MediaStore(
            base_dir=storage_dir,
            max_file_size=config.max_file_size_mb * 1024 * 1024,
            retention_days=config.retention_days,
        )
        self._transcription = TranscriptionService(
            backend=config.transcription.backend,
            model=config.transcription.model,
            api_key=config.transcription.api_key,
        )
        self._vision = VisionProcessor(
            provider="anthropic",
            max_pixels=config.vision.max_pixels,
            quality=config.vision.quality,
        )
        logger.info("Media services enabled (storage: %s)", storage_dir)

    def process_attachments(
        self,
        text: str,
        attachments: list[Attachment] | None,
        sender_id: str,
        channel: str = "unknown",
    ) -> tuple[str, list[dict]]:
        """Process media attachments, returning updated text and image content blocks.

        Voice/audio attachments are transcribed and their text is prepended to the
        user message. Image attachments are converted to LLM content blocks.

        Returns:
            (updated_text, image_content_blocks) — text with transcriptions prepended
            and a list of image content block dicts for the LLM.
        """
        if not attachments:
            return text, []

        voice_parts: list[str] = []
        image_blocks: list[dict] = []

        for attachment in attachments:
            if attachment.type in (AttachmentType.VOICE, AttachmentType.AUDIO):
                self._process_voice(attachment, sender_id, voice_parts, channel)
            elif attachment.type == AttachmentType.IMAGE:
                self._process_image(attachment, sender_id, image_blocks, channel)

        if voice_parts:
            voice_text = "\n".join(voice_parts)
            text = f"{voice_text}\n{text}" if text else voice_text

        return text, image_blocks

    def _process_voice(
        self,
        attachment: Attachment,
        sender_id: str,
        voice_parts: list[str],
        channel: str = "unknown",
    ) -> None:
        """Save, transcribe a voice attachment and append to voice_parts."""
        try:
            saved_path = self._store.save_media(attachment, channel=channel)
        except Exception:
            logger.warning("Failed to save voice attachment", exc_info=True)
            voice_parts.append("[Voice message: could not save audio file]")
            return

        transcribed = self._transcription.transcribe(saved_path)
        if transcribed:
            voice_parts.append(f"[Voice message]: {transcribed}")
        else:
            voice_parts.append(f"[Voice message: transcription failed] (file: {saved_path.name})")

    def _process_image(
        self,
        attachment: Attachment,
        sender_id: str,
        image_blocks: list[dict],
        channel: str = "unknown",
    ) -> None:
        """Save an image attachment and prepare it as an LLM content block."""
        try:
            saved_path = self._store.save_media(attachment, channel=channel)
        except Exception:
            logger.warning("Failed to save image attachment", exc_info=True)
            return

        content_block = self._vision.prepare_image(saved_path)
        if content_block is not None:
            image_blocks.append(content_block)
        else:
            logger.warning("Image could not be processed: %s", saved_path)
