#!/usr/bin/env python3
"""Host bridge server for macOS-native tools.

Lightweight FastAPI server that runs on the host (not in containers) and 
executes CLI commands like memo and remindctl on behalf of containerized executors.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Scoped auth tokens by tool group
SCOPED_TOKENS: dict[str, str] = {}

security = HTTPBearer()


class BridgeResponse(BaseModel):
    """Standard bridge response format."""
    
    ok: bool
    output: str = ""
    error: str = ""
    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class NotesListRequest(BaseModel):
    """Request for listing notes."""
    
    folder: str | None = None


class NotesSearchRequest(BaseModel):
    """Request for searching notes."""
    
    query: str


class NotesCreateRequest(BaseModel):
    """Request for creating a note."""
    
    title: str
    body: str
    folder: str | None = None


class RemindersListRequest(BaseModel):
    """Request for listing reminders."""
    
    filter: str | None = "all"  # today|week|overdue|all


class RemindersAddRequest(BaseModel):
    """Request for adding a reminder."""
    
    title: str
    list: str | None = None
    due: str | None = None


class RemindersCompleteRequest(BaseModel):
    """Request for completing a reminder."""
    
    id: str


# Things 3 request models
class ThingsInboxRequest(BaseModel):
    """Request for Things 3 inbox."""
    
    limit: int = 50


class ThingsSearchRequest(BaseModel):
    """Request for searching Things 3."""
    
    query: str


class ThingsAddRequest(BaseModel):
    """Request for adding a Things 3 item."""
    
    title: str
    notes: str | None = None
    tags: str | None = None
    when: str | None = None
    list: str | None = None
    heading: str | None = None


class ThingsUpdateRequest(BaseModel):
    """Request for updating a Things 3 item."""
    
    id: str
    completed: bool | None = None
    title: str | None = None
    notes: str | None = None
    tags: str | None = None


# iMessage request models
class IMessageRecentRequest(BaseModel):
    """Request for recent iMessages."""
    
    limit: int = 20


class IMessageSendRequest(BaseModel):
    """Request for sending an iMessage."""
    
    to: str
    text: str


def get_required_scope(request_path: str) -> str:
    """Determine required token scope based on request path."""
    if request_path.startswith("/notes/"):
        return "NOTES"
    elif request_path.startswith("/reminders/"):
        return "REMINDERS"
    elif request_path.startswith("/things/"):
        return "THINGS"
    elif request_path.startswith("/imessage/"):
        return "IMESSAGE"
    else:
        return "UNKNOWN"


def create_scoped_authenticator(required_scope: str):
    """Create a scoped authenticator for a specific tool group."""
    def authenticate_scope(credentials: HTTPAuthorizationCredentials = Depends(security)) -> bool:
        """Validate the scoped bearer token."""
        if not SCOPED_TOKENS:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Bridge server not properly initialized"
            )
        
        expected_token = SCOPED_TOKENS.get(required_scope)
        if not expected_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"No token configured for scope {required_scope}"
            )
        
        if credentials.credentials != expected_token:
            logger.warning("Invalid auth token attempted for scope %s", required_scope)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token"
            )
        
        logger.debug("Authenticated request for scope %s", required_scope)
        return True
    
    return authenticate_scope


# Create scoped authenticators for each tool group
authenticate_notes = create_scoped_authenticator("NOTES")
authenticate_reminders = create_scoped_authenticator("REMINDERS")
authenticate_things = create_scoped_authenticator("THINGS")
authenticate_imessage = create_scoped_authenticator("IMESSAGE")


def run_command(cmd: list[str], timeout: int = 30) -> BridgeResponse:
    """Execute a CLI command safely using subprocess.
    
    Args:
        cmd: Command as list of strings (never shell=True)
        timeout: Timeout in seconds
    
    Returns:
        BridgeResponse with command output or error
    """
    execution_id = str(uuid.uuid4())
    logger.info("Executing command: %s (execution_id=%s)", " ".join(cmd), execution_id)
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False  # Don't raise on non-zero exit codes
        )
        
        if result.returncode == 0:
            logger.info("Command succeeded (execution_id=%s)", execution_id)
            return BridgeResponse(
                ok=True,
                output=result.stdout.strip(),
                execution_id=execution_id
            )
        else:
            logger.warning(
                "Command failed with exit code %d (execution_id=%s): %s",
                result.returncode, execution_id, result.stderr.strip()
            )
            return BridgeResponse(
                ok=False,
                error=f"Command failed with exit code {result.returncode}: {result.stderr.strip()}",
                execution_id=execution_id
            )
    
    except subprocess.TimeoutExpired:
        logger.error("Command timed out after %ds (execution_id=%s)", timeout, execution_id)
        return BridgeResponse(
            ok=False,
            error=f"Command timed out after {timeout} seconds",
            execution_id=execution_id
        )
    except FileNotFoundError:
        logger.error("Command not found: %s (execution_id=%s)", cmd[0], execution_id)
        return BridgeResponse(
            ok=False,
            error=f"Command not found: {cmd[0]}",
            execution_id=execution_id
        )
    except Exception as e:
        logger.error("Unexpected error running command (execution_id=%s): %s", execution_id, e)
        return BridgeResponse(
            ok=False,
            error=f"Unexpected error: {str(e)}",
            execution_id=execution_id
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan manager."""
    global SCOPED_TOKENS
    
    # Generate scoped auth tokens
    scopes = ["NOTES", "REMINDERS", "THINGS", "IMESSAGE"]
    
    for scope in scopes:
        env_var = f"BRIDGE_TOKEN_{scope}"
        token = os.environ.get(env_var)
        if not token:
            token = secrets.token_urlsafe(32)
            logger.info("Generated %s bridge token: %s", scope.lower(), token)
            logger.warning("Consider setting %s environment variable to persist this token", env_var)
        else:
            logger.info("Using configured %s bridge token", scope.lower())
        
        SCOPED_TOKENS[scope] = token
    
    logger.info("Bridge server ready on %s:%s with %d scoped tokens", 
               os.environ.get("BRIDGE_HOST", "127.0.0.1"),
               os.environ.get("BRIDGE_PORT", "8099"),
               len(SCOPED_TOKENS))
    
    yield
    
    logger.info("Bridge server shutting down")


# Create FastAPI app
app = FastAPI(
    title="Creel Host Bridge",
    description="HTTP bridge for macOS-native tools",
    version="1.0.0",
    lifespan=lifespan
)


# Health check endpoint (no auth required)
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "creel-bridge"}


# Notes endpoints (via memo CLI)
@app.post("/notes/list", response_model=BridgeResponse)
async def notes_list(
    request: NotesListRequest = NotesListRequest(),
    _: bool = Depends(authenticate_notes)
) -> BridgeResponse:
    """List notes via memo CLI."""
    cmd = ["memo", "notes"]
    if request.folder:
        cmd.extend(["-f", request.folder])
    
    return run_command(cmd)


@app.post("/notes/search", response_model=BridgeResponse)
async def notes_search(
    request: NotesSearchRequest,
    _: bool = Depends(authenticate_notes)
) -> BridgeResponse:
    """Search notes via memo CLI."""
    cmd = ["memo", "notes", "-s", request.query]
    return run_command(cmd)


@app.post("/notes/create", response_model=BridgeResponse)
async def notes_create(
    request: NotesCreateRequest,
    _: bool = Depends(authenticate_notes)
) -> BridgeResponse:
    """Create a note via memo CLI."""
    cmd = ["memo", "add", request.title]
    if request.body:
        cmd.extend(["-b", request.body])
    if request.folder:
        cmd.extend(["-f", request.folder])
    
    return run_command(cmd)


# Reminders endpoints (via remindctl CLI)
@app.post("/reminders/list", response_model=BridgeResponse)
async def reminders_list(
    request: RemindersListRequest = RemindersListRequest(),
    _: bool = Depends(authenticate_reminders)
) -> BridgeResponse:
    """List reminders via remindctl CLI."""
    if request.filter == "all":
        cmd = ["remindctl", "all"]
    elif request.filter == "today":
        cmd = ["remindctl", "today"]
    elif request.filter == "week":
        cmd = ["remindctl", "week"]
    elif request.filter == "overdue":
        cmd = ["remindctl", "overdue"]
    else:
        # Default to plain remindctl
        cmd = ["remindctl"]
    
    return run_command(cmd)


@app.post("/reminders/add", response_model=BridgeResponse)
async def reminders_add(
    request: RemindersAddRequest,
    _: bool = Depends(authenticate_reminders)
) -> BridgeResponse:
    """Add a reminder via remindctl CLI."""
    cmd = ["remindctl", "add", request.title]
    if request.list:
        cmd.extend(["-l", request.list])
    if request.due:
        cmd.extend(["-d", request.due])
    
    return run_command(cmd)


@app.post("/reminders/complete", response_model=BridgeResponse)
async def reminders_complete(
    request: RemindersCompleteRequest,
    _: bool = Depends(authenticate_reminders)
) -> BridgeResponse:
    """Complete a reminder via remindctl CLI."""
    cmd = ["remindctl", "complete", request.id]
    return run_command(cmd)


# Things 3 endpoints (via things CLI)
@app.post("/things/inbox", response_model=BridgeResponse)
async def things_inbox(
    request: ThingsInboxRequest = ThingsInboxRequest(),
    _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Get Things 3 inbox via things CLI."""
    cmd = ["things", "inbox", "--limit", str(request.limit)]
    return run_command(cmd)


@app.post("/things/today", response_model=BridgeResponse)
async def things_today(
    _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Get Things 3 today list via things CLI."""
    cmd = ["things", "today"]
    return run_command(cmd)


@app.post("/things/upcoming", response_model=BridgeResponse)
async def things_upcoming(
    _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Get Things 3 upcoming list via things CLI."""
    cmd = ["things", "upcoming"]
    return run_command(cmd)


@app.post("/things/search", response_model=BridgeResponse)
async def things_search(
    request: ThingsSearchRequest,
    _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Search Things 3 via things CLI."""
    cmd = ["things", "search", request.query]
    return run_command(cmd)


@app.post("/things/projects", response_model=BridgeResponse)
async def things_projects(
    _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Get Things 3 projects via things CLI."""
    cmd = ["things", "projects"]
    return run_command(cmd)


@app.post("/things/add", response_model=BridgeResponse)
async def things_add(
    request: ThingsAddRequest,
    _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Add item to Things 3 via things CLI."""
    cmd = ["things", "add", request.title]
    
    if request.notes:
        cmd.extend(["--notes", request.notes])
    if request.tags:
        cmd.extend(["--tags", request.tags])
    if request.when:
        cmd.extend(["--when", request.when])
    if request.list:
        cmd.extend(["--list", request.list])
    if request.heading:
        cmd.extend(["--heading", request.heading])
    
    return run_command(cmd)


@app.post("/things/update", response_model=BridgeResponse)
async def things_update(
    request: ThingsUpdateRequest,
    _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Update Things 3 item via things CLI."""
    cmd = ["things", "update", "--id", request.id]
    
    if request.completed is not None:
        cmd.extend(["--completed", "true" if request.completed else "false"])
    if request.title:
        cmd.extend(["--title", request.title])
    if request.notes:
        cmd.extend(["--notes", request.notes])
    if request.tags:
        cmd.extend(["--tags", request.tags])
    
    return run_command(cmd)


# iMessage endpoints (via imsg CLI)
def _check_imsg_available() -> bool:
    """Check if imsg CLI is available."""
    import os
    return os.path.exists("/opt/homebrew/bin/imsg")


@app.post("/imessage/recent", response_model=BridgeResponse)
async def imessage_recent(
    request: IMessageRecentRequest = IMessageRecentRequest(),
    _: bool = Depends(authenticate_imessage)
) -> BridgeResponse:
    """Get recent iMessages via imsg CLI."""
    if not _check_imsg_available():
        return BridgeResponse(
            ok=False,
            error="imsg CLI not found at /opt/homebrew/bin/imsg"
        )
    
    cmd = ["/opt/homebrew/bin/imsg", "recent", "--limit", str(request.limit)]
    return run_command(cmd)


@app.post("/imessage/send", response_model=BridgeResponse)
async def imessage_send(
    request: IMessageSendRequest,
    _: bool = Depends(authenticate_imessage)
) -> BridgeResponse:
    """Send iMessage via imsg CLI."""
    if not _check_imsg_available():
        return BridgeResponse(
            ok=False,
            error="imsg CLI not found at /opt/homebrew/bin/imsg"
        )
    
    cmd = ["/opt/homebrew/bin/imsg", "send", "--to", request.to, "--text", request.text]
    return run_command(cmd)


@app.post("/imessage/chats", response_model=BridgeResponse)
async def imessage_chats(
    _: bool = Depends(authenticate_imessage)
) -> BridgeResponse:
    """Get iMessage chats via imsg CLI."""
    if not _check_imsg_available():
        return BridgeResponse(
            ok=False,
            error="imsg CLI not found at /opt/homebrew/bin/imsg"
        )
    
    cmd = ["/opt/homebrew/bin/imsg", "chats"]
    return run_command(cmd)


def main():
    """Run the bridge server."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Creel Host Bridge Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8099, help="Port to bind to")
    parser.add_argument("--log-level", default="INFO", help="Log level")
    
    args = parser.parse_args()
    
    # Set up logging
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Store host/port in env for lifespan
    os.environ["BRIDGE_HOST"] = args.host
    os.environ["BRIDGE_PORT"] = str(args.port)
    
    logger.info("Starting Creel Host Bridge Server on %s:%d", args.host, args.port)
    
    uvicorn.run(
        "bridge.server:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        access_log=True
    )


if __name__ == "__main__":
    main()