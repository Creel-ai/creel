"""OAuth credential hygiene — host-side Google token minting and tracking.

Refresh tokens and OAuth client secrets remain in the taskrunner process.
Executors should only receive short-lived access tokens.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

# Module-level tracking of token refresh timestamps and cached tokens.
_token_refresh_log: dict[str, float] = {}
_token_cache: dict[str, str] = {}
_lock = threading.Lock()

# Default max token age: 1 hour (Google's default access token lifetime)
DEFAULT_MAX_TOKEN_AGE = 3600


def _build_refreshable_credentials(credentials_json: str, source: str) -> Credentials:
    """Validate OAuth JSON and build a refreshable credential object."""
    from google.oauth2.credentials import Credentials

    try:
        creds_data = json.loads(credentials_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {source}: {e}") from e

    required_fields = ["refresh_token", "client_id", "client_secret"]
    missing = [f for f in required_fields if f not in creds_data]
    if missing:
        raise RuntimeError(f"Missing required fields in {source}: {missing}")

    return Credentials(
        token=None,
        refresh_token=creds_data["refresh_token"],
        client_id=creds_data["client_id"],
        client_secret=creds_data["client_secret"],
        token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
    )


def get_google_access_token_from_json(
    credentials_json: str,
    *,
    cache_key: str = "GOOGLE_CREDENTIALS_JSON",
    max_token_age_seconds: int = DEFAULT_MAX_TOKEN_AGE,
    force_refresh: bool = True,
) -> str:
    """Mint (or reuse) a short-lived Google OAuth access token."""
    from google.auth.transport.requests import Request

    with _lock:
        last_refresh = _token_refresh_log.get(cache_key, 0)
        cached_token = _token_cache.get(cache_key)
    token_age = time.time() - last_refresh if last_refresh else float("inf")

    should_refresh = (
        force_refresh or not cached_token or token_age > max_token_age_seconds
    )
    if should_refresh:
        creds = _build_refreshable_credentials(credentials_json, source=cache_key)
        try:
            creds.refresh(Request())
        except Exception as e:
            raise RuntimeError(f"Token refresh failed for {cache_key}: {e}") from e

        if not creds.token:
            raise RuntimeError(
                f"Token refresh failed for {cache_key}: no access token returned"
            )

        with _lock:
            _token_refresh_log[cache_key] = time.time()
            _token_cache[cache_key] = creds.token
        logger.debug(
            "OAuth token refreshed for %s (age was %.0fs, max=%ds)",
            cache_key,
            token_age,
            max_token_age_seconds,
        )
        return creds.token

    logger.debug(
        "OAuth token for %s still fresh (age=%.0fs, max=%ds)",
        cache_key,
        token_age,
        max_token_age_seconds,
    )
    assert cached_token is not None
    return cached_token


def get_google_credentials(
    *,
    env_var: str = "GOOGLE_CREDENTIALS_JSON",
    max_token_age_seconds: int = DEFAULT_MAX_TOKEN_AGE,
    force_refresh: bool = True,
) -> Credentials:
    """Build Google OAuth credentials from a host-minted access token.

    Args:
        env_var: Environment variable containing Google OAuth credentials JSON.
        max_token_age_seconds: Maximum age for access tokens before forced refresh.
        force_refresh: If True (default), always refresh the token.

    Returns:
        Google OAuth Credentials object containing only an access token.

    Raises:
        RuntimeError: If credentials JSON is missing or invalid.
        RuntimeError: If token refresh fails.
    """
    from google.oauth2.credentials import Credentials

    creds_json = os.environ.get(env_var)
    if not creds_json:
        raise RuntimeError(f"{env_var} environment variable not set")

    token = get_google_access_token_from_json(
        creds_json,
        cache_key=env_var,
        max_token_age_seconds=max_token_age_seconds,
        force_refresh=force_refresh,
    )
    return Credentials(token=token)


def check_credential_freshness(
    env_var: str = "GOOGLE_CREDENTIALS_JSON",
    max_age_seconds: int = DEFAULT_MAX_TOKEN_AGE,
) -> dict:
    """Check the freshness status of a cached credential.

    Returns a dict with:
        - fresh: bool — whether the token is within max_age
        - last_refresh: float — timestamp of last refresh (0 if never)
        - age_seconds: float — seconds since last refresh
        - max_age_seconds: int — configured maximum age
    """
    with _lock:
        last_refresh = _token_refresh_log.get(env_var, 0)
    age = time.time() - last_refresh if last_refresh else float("inf")

    return {
        "fresh": age <= max_age_seconds,
        "last_refresh": last_refresh,
        "age_seconds": age if last_refresh else None,
        "max_age_seconds": max_age_seconds,
    }


def clear_token_cache() -> None:
    """Clear all cached token refresh timestamps.

    Call this when rotating credentials or during cleanup.
    """
    with _lock:
        _token_refresh_log.clear()
        _token_cache.clear()
    logger.info("OAuth token cache cleared")
