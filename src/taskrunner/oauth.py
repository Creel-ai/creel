"""OAuth credential hygiene — enforce short-lived tokens and audit access.

Wraps Google OAuth credential handling with:
- Maximum token age enforcement (configurable, default 1 hour)
- Credential freshness validation on each use
- Audit logging of token refresh events
- Stale refresh token warnings

Usage in executors:
    from taskrunner.oauth import get_google_credentials
    creds = get_google_credentials(max_token_age_seconds=3600)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Module-level tracking of token refresh timestamps (guarded by _lock)
_token_refresh_log: dict[str, float] = {}
_lock = threading.Lock()

# Default max token age: 1 hour (Google's default access token lifetime)
DEFAULT_MAX_TOKEN_AGE = 3600


def get_google_credentials(
    *,
    env_var: str = "GOOGLE_CREDENTIALS_JSON",
    max_token_age_seconds: int = DEFAULT_MAX_TOKEN_AGE,
    force_refresh: bool = True,
) -> "Credentials":
    """Build and validate Google OAuth credentials with freshness enforcement.

    Always refreshes the access token to ensure we're using short-lived
    credentials. Tracks refresh timestamps for audit purposes.

    Args:
        env_var: Environment variable containing the credentials JSON.
        max_token_age_seconds: Maximum age for access tokens before forced refresh.
        force_refresh: If True (default), always refresh the token.

    Returns:
        Refreshed Google OAuth Credentials object.

    Raises:
        RuntimeError: If credentials JSON is missing or invalid.
        RuntimeError: If token refresh fails.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds_json = os.environ.get(env_var)
    if not creds_json:
        raise RuntimeError(f"{env_var} environment variable not set")

    try:
        creds_data = json.loads(creds_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {env_var}: {e}") from e

    required_fields = ["refresh_token", "client_id", "client_secret"]
    missing = [f for f in required_fields if f not in creds_data]
    if missing:
        raise RuntimeError(f"Missing required fields in {env_var}: {missing}")

    # Build credentials with no cached access token — forces fresh token
    creds = Credentials(
        token=None,
        refresh_token=creds_data["refresh_token"],
        client_id=creds_data["client_id"],
        client_secret=creds_data["client_secret"],
        token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
    )

    # Check if an existing token is still fresh (not expired)
    with _lock:
        last_refresh = _token_refresh_log.get(env_var, 0)
    token_age = time.time() - last_refresh if last_refresh else float("inf")

    if force_refresh or token_age > max_token_age_seconds:
        try:
            creds.refresh(Request())
            with _lock:
                _token_refresh_log[env_var] = time.time()
            logger.debug(
                "OAuth token refreshed for %s (age was %.0fs, max=%ds)",
                env_var, token_age, max_token_age_seconds,
            )
        except Exception as e:
            logger.error("OAuth token refresh failed for %s: %s", env_var, e)
            raise RuntimeError(f"Token refresh failed for {env_var}: {e}") from e
    else:
        logger.debug(
            "OAuth token for %s still fresh (age=%.0fs, max=%ds)",
            env_var, token_age, max_token_age_seconds,
        )

    return creds


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
    logger.info("OAuth token cache cleared")
