"""Shared Google credential utility for executors.

Executors receive only a short-lived access token from taskrunner. Refresh
tokens and client secrets must never be present in executor runtime env vars.
"""

from __future__ import annotations

import os


def get_credentials(
    *,
    env_var: str = "GOOGLE_ACCESS_TOKEN",
):
    """Build Google OAuth credentials from an injected access token."""
    from google.oauth2.credentials import Credentials

    access_token = os.environ.get(env_var)
    if not access_token:
        raise RuntimeError(f"{env_var} environment variable not set")

    return Credentials(token=access_token)
