"""Dashboard API authentication.

All /api/* endpoints require a Bearer token. The token is auto-generated
on first daemon start and stored in ~/.creel/dashboard-token.

/health and /v1/* endpoints remain unauthenticated for backward compatibility.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


def _token_path() -> Path:
    """Return path to the dashboard token file."""
    creel_home = Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))
    return creel_home / "dashboard-token"


def ensure_dashboard_token() -> str:
    """Load or generate the dashboard auth token.

    If the token file doesn't exist, generate a new UUID4 token and write it.
    Returns the token string.
    """
    path = _token_path()
    if path.is_file():
        token = path.read_text().strip()
        if token:
            return token

    # Generate new token
    token = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    # Restrict permissions to owner only
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


# Module-level token loaded on first import in the daemon process
_bearer_scheme = HTTPBearer(auto_error=False)


async def require_dashboard_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    token: str | None = Query(None, alias="token"),
) -> str:
    """FastAPI dependency that validates the dashboard auth token.

    Accepts token via:
    - Authorization: Bearer <token> header
    - ?token=<token> query parameter

    Raises 401 if token is missing or invalid.
    """
    expected = request.app.state.dashboard_token

    # Check Bearer header first
    if credentials and credentials.credentials == expected:
        return expected

    # Check query parameter
    if token and token == expected:
        return expected

    raise HTTPException(
        status_code=401,
        detail="unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )
