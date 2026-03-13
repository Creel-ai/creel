"""MediaHandlerMixin — shared media download/upload/conversion utilities."""

from __future__ import annotations

import logging
from collections.abc import Callable

from creel.channels.message import Attachment, AttachmentType

logger = logging.getLogger(__name__)

# Default MIME-prefix-to-AttachmentType mapping (shared across channels).
DEFAULT_MIME_PREFIX_MAP: dict[str, AttachmentType] = {
    "image/": AttachmentType.IMAGE,
    "audio/": AttachmentType.AUDIO,
    "video/": AttachmentType.VIDEO,
}

# Audio MIME types that indicate a voice memo rather than a regular audio file.
# NOTE: Keep in sync with channel-specific overrides.  Channels that need
# additional types (e.g. audio/ogg for Telegram) should override
# ``_voice_mime_types`` on the subclass.
DEFAULT_VOICE_MIME_TYPES: frozenset[str] = frozenset({"audio/x-caf", "audio/caf", "audio/amr"})


class MediaHandlerMixin:
    """Mixin providing shared media classification and conversion helpers.

    Channels that deal with media attachments can inherit this to get a
    consistent ``_classify_mime_type`` helper and extensible type maps.

    Override the class attributes to customise behaviour per-channel:

    - ``_mime_prefix_map``: maps MIME prefixes to :class:`AttachmentType`.
    - ``_voice_mime_types``: MIME types treated as voice messages.
    - ``_platform_type_map``: maps platform-specific type strings
      (e.g. ``"photo"``, ``"voice"``) to :class:`AttachmentType`.
    """

    _mime_prefix_map: dict[str, AttachmentType] = DEFAULT_MIME_PREFIX_MAP
    _voice_mime_types: frozenset[str] = DEFAULT_VOICE_MIME_TYPES
    _platform_type_map: dict[str, AttachmentType] = {}

    def _classify_mime_type(self, mime_type: str | None) -> AttachmentType:
        """Return the :class:`AttachmentType` for a given MIME type string.

        Voice MIME types take priority, then prefix matching, then FILE.
        """
        if not mime_type:
            return AttachmentType.FILE
        if mime_type in self._voice_mime_types:
            return AttachmentType.VOICE
        for prefix, att_type in self._mime_prefix_map.items():
            if mime_type.startswith(prefix):
                return att_type
        return AttachmentType.FILE

    def _classify_platform_type(self, platform_type: str) -> AttachmentType:
        """Map a platform-specific file type string to :class:`AttachmentType`.

        Falls back to ``FILE`` if the type is not in ``_platform_type_map``.
        """
        return self._platform_type_map.get(platform_type, AttachmentType.FILE)

    def _download_and_classify(
        self,
        download_fn: Callable[[str], bytes],
        file_id: str,
        *,
        platform_type: str | None = None,
        mime_type: str | None = None,
        file_name: str | None = None,
        file_size: int | None = None,
    ) -> Attachment | None:
        """Download a file and return a classified :class:`Attachment`.

        Parameters
        ----------
        download_fn:
            Callable that takes a *file_id* and returns ``bytes``.
        file_id:
            Platform-specific file identifier.
        platform_type:
            Optional platform type hint (e.g. ``"photo"``).
        mime_type:
            Optional MIME type.
        file_name:
            Optional original file name.
        file_size:
            Optional file size in bytes.

        Returns ``None`` if the download fails.
        """
        try:
            data = download_fn(file_id)
        except Exception:
            logger.warning("Failed to download file %s", file_id, exc_info=True)
            return None

        if platform_type:
            att_type = self._classify_platform_type(platform_type)
        else:
            att_type = self._classify_mime_type(mime_type)

        return Attachment(
            type=att_type,
            data=data,
            mime_type=mime_type,
            file_name=file_name,
            file_size=file_size or len(data),
        )
