"""Post-execution credential scanner — detect leaked secrets in tool output.

Scans tool results for common credential patterns (API keys, tokens,
passwords) that should never be exposed to the LLM context.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CredentialMatch:
    """A detected credential pattern in tool output."""

    pattern_name: str
    matched_text: str  # Redacted — first 4 chars + "..." + last 2 chars
    position: int  # Character offset in the text


# Credential patterns ordered by specificity (most specific first)
# Each tuple: (name, compiled regex, minimum_match_length)
_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern, int]] = [
    # AWS
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}"), 20),
    ("aws_secret_key", re.compile(r"(?:aws_secret_access_key|secret_key)\s*[=:]\s*[A-Za-z0-9/+=]{40}"), 40),
    # Google
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z\-_]{35}"), 39),
    ("google_oauth_token", re.compile(r"ya29\.[0-9A-Za-z\-_]+"), 20),
    # GitHub
    ("github_token", re.compile(r"gh[ps]_[A-Za-z0-9_]{36,}"), 40),
    ("github_fine_grained", re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), 30),
    # Anthropic
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{40,}"), 45),
    # OpenAI
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{20,}"), 23),
    # Slack
    ("slack_bot_token", re.compile(r"xoxb-[0-9]{10,}-[0-9A-Za-z]{20,}"), 30),
    ("slack_user_token", re.compile(r"xoxp-[0-9]{10,}-[0-9A-Za-z]{20,}"), 30),
    # Stripe
    ("stripe_secret_key", re.compile(r"sk_live_[0-9a-zA-Z]{24,}"), 32),
    ("stripe_restricted_key", re.compile(r"rk_live_[0-9a-zA-Z]{24,}"), 32),
    # Generic patterns (lower priority, higher false positive risk)
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), 20),
    ("basic_auth", re.compile(r"Basic\s+[A-Za-z0-9+/]+=+", re.IGNORECASE), 15),
    # Private keys
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"), 20),
    # Generic key=value patterns (most likely to false-positive, check last)
    ("generic_api_key", re.compile(
        r"(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|secret[_-]?key)"
        r"\s*[=:]\s*['\"]?[A-Za-z0-9\-._~+/]{20,}['\"]?",
        re.IGNORECASE,
    ), 25),
    ("generic_password", re.compile(
        r"(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{8,}['\"]?",
        re.IGNORECASE,
    ), 12),
]


def _redact(text: str) -> str:
    """Redact a credential value, keeping only first 4 and last 2 chars."""
    if len(text) <= 8:
        return text[:2] + "***"
    return text[:4] + "..." + text[-2:]


def scan_for_credentials(text: str) -> list[CredentialMatch]:
    """Scan text for credential patterns.

    Returns a list of CredentialMatch objects for any detected patterns.
    An empty list means no credentials were found.
    """
    if not text:
        return []

    matches: list[CredentialMatch] = []
    seen_positions: set[int] = set()

    for pattern_name, regex, min_length in _CREDENTIAL_PATTERNS:
        for match in regex.finditer(text):
            matched = match.group(0)
            if len(matched) < min_length:
                continue

            # Avoid duplicate matches at the same position
            pos = match.start()
            if pos in seen_positions:
                continue
            seen_positions.add(pos)

            matches.append(CredentialMatch(
                pattern_name=pattern_name,
                matched_text=_redact(matched),
                position=pos,
            ))
            logger.warning(
                "Credential detected in output: %s at position %d (%s)",
                pattern_name, pos, _redact(matched),
            )

    return matches


def redact_credentials(text: str) -> tuple[str, list[CredentialMatch]]:
    """Scan text and redact any detected credentials in-place.

    Returns:
        A tuple of (redacted_text, list_of_matches).
        If no credentials found, returns the original text unchanged.
    """
    if not text:
        return text, []

    matches: list[CredentialMatch] = []
    # Collect all match spans for replacement
    replacements: list[tuple[int, int, str, str]] = []

    for pattern_name, regex, min_length in _CREDENTIAL_PATTERNS:
        for match in regex.finditer(text):
            matched = match.group(0)
            if len(matched) < min_length:
                continue

            replacements.append((
                match.start(),
                match.end(),
                pattern_name,
                matched,
            ))

    if not replacements:
        return text, []

    # Sort by position and deduplicate overlapping ranges
    replacements.sort(key=lambda r: r[0])
    deduped: list[tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, name, matched in replacements:
        if start >= last_end:
            deduped.append((start, end, name, matched))
            last_end = end

    # Build redacted text from back to front to preserve positions
    redacted = text
    for start, end, name, matched in reversed(deduped):
        replacement = f"[REDACTED:{name}]"
        redacted = redacted[:start] + replacement + redacted[end:]
        matches.append(CredentialMatch(
            pattern_name=name,
            matched_text=_redact(matched),
            position=start,
        ))

    matches.reverse()  # Restore original order
    return redacted, matches
