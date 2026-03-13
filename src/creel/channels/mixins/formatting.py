"""FormattingMixin — message formatting and chunking per platform."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class FormattingMixin:
    """Mixin for platform-aware message formatting and chunking.

    Channels inherit this to get consistent text splitting when the
    platform imposes a maximum message length (e.g. Telegram's 4096 chars).

    Class attributes (override per-channel):

    - ``_max_message_length``: maximum characters per message (0 = no limit).
    """

    _max_message_length: int = 0

    def _chunk_text(self, text: str, limit: int | None = None) -> list[str]:
        """Split *text* into chunks of at most *limit* characters.

        Breaks at newlines when possible so messages stay readable.
        If *limit* is ``None``, falls back to ``_max_message_length``.
        A limit of 0 means no splitting.
        """
        effective_limit = limit if limit is not None else self._max_message_length
        if not effective_limit or len(text) <= effective_limit:
            return [text]

        chunks: list[str] = []
        while text:
            if len(text) <= effective_limit:
                chunks.append(text)
                break
            # Try to break at a newline within the limit
            break_at = text.rfind("\n", 0, effective_limit)
            if break_at <= 0:
                break_at = effective_limit
            chunks.append(text[:break_at])
            text = text[break_at:].lstrip("\n")
        return chunks

    def _truncate(self, text: str, limit: int | None = None, suffix: str = "...") -> str:
        """Truncate *text* to *limit* characters, appending *suffix* if trimmed."""
        effective_limit = limit if limit is not None else self._max_message_length
        if not effective_limit or len(text) <= effective_limit:
            return text
        return text[: effective_limit - len(suffix)] + suffix
