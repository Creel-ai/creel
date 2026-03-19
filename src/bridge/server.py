#!/usr/bin/env python3
"""Host bridge server for macOS-native tools.

Lightweight FastAPI server that runs on the host (not in containers) and
executes CLI commands like memo and remindctl on behalf of containerized executors.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from bridge.browser import SessionDead
from bridge.process_manager import ProcessManager

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 1_000_000

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


# Browser request models
class BrowserConnectRequest(BaseModel):
    """Request for creating a browser session."""

    mode: str = "managed"  # "managed" | "relay" | "native"
    cdp_url: str | None = None
    headless: bool = True


class BrowserNavigateRequest(BaseModel):
    """Request for navigating to a URL."""

    session_id: str
    url: str


class BrowserContentRequest(BaseModel):
    """Request for getting page content."""

    session_id: str
    selector: str | None = None


class BrowserClickRequest(BaseModel):
    """Request for clicking an element."""

    session_id: str
    selector: str


class BrowserTypeRequest(BaseModel):
    """Request for typing text into an input."""

    session_id: str
    selector: str
    text: str


class BrowserScreenshotRequest(BaseModel):
    """Request for taking a screenshot."""

    session_id: str
    full_page: bool = False


class BrowserLinksRequest(BaseModel):
    """Request for getting page links."""

    session_id: str


class BrowserCloseRequest(BaseModel):
    """Request for closing a browser session."""

    session_id: str


def _validate_git_ref(value: str) -> str:
    """Validate that a git ref name does not look like a flag."""
    if value.startswith("-"):
        raise ValueError("must not start with '-'")
    return value


# iMessage request models
class GitStatusRequest(BaseModel):
    """Request for git status."""

    short: bool = False


class GitDiffRequest(BaseModel):
    """Request for git diff."""

    cached: bool = False
    path: str | None = None

    @field_validator("path")
    @classmethod
    def path_no_traversal(cls, v: str | None) -> str | None:
        if v is not None and ".." in PurePosixPath(v).parts:
            raise ValueError("path must not contain '..' traversal")
        return v


class GitLogRequest(BaseModel):
    """Request for git log."""

    max_count: int = Field(default=10, le=500)
    oneline: bool = True


class GitCommitRequest(BaseModel):
    """Request for git commit."""

    message: str = Field(max_length=50_000)
    all: bool = False


class GitBranchRequest(BaseModel):
    """Request for git branch operations."""

    name: str | None = None
    delete: bool = False
    list_all: bool = False

    @field_validator("name")
    @classmethod
    def name_not_flag(cls, v: str | None) -> str | None:
        if v is not None:
            _validate_git_ref(v)
        return v


class GitPushRequest(BaseModel):
    """Request for git push."""

    remote: str = "origin"
    branch: str | None = None
    set_upstream: bool = False

    @field_validator("remote")
    @classmethod
    def remote_not_flag(cls, v: str) -> str:
        _validate_git_ref(v)
        if not re.fullmatch(r"[a-zA-Z0-9_.\-]+", v):
            raise ValueError("remote must be a named remote, not a URL")
        return v

    @field_validator("branch")
    @classmethod
    def branch_not_flag(cls, v: str | None) -> str | None:
        if v is not None:
            _validate_git_ref(v)
        return v


class ExecRequest(BaseModel):
    """Request for spawning a command on the host."""

    command: str = Field(min_length=1, max_length=65536)
    background: bool = False
    workdir: str | None = None
    timeout: int = Field(default=300, le=3600)
    env: dict[str, str] | None = None

    @field_validator("workdir")
    @classmethod
    def workdir_no_traversal(cls, v: str | None) -> str | None:
        if v is not None and ".." in PurePosixPath(v).parts:
            raise ValueError("workdir must not contain '..' traversal")
        return v


class ProcessActionRequest(BaseModel):
    """Request for managing a running process."""

    session_id: str
    action: str = Field(pattern=r"^(log|poll|write|kill)$")
    limit: int = Field(default=100, le=5000)
    offset: int = Field(default=0, ge=0)
    data: str | None = Field(default=None, max_length=1_048_576)  # 1 MB


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
    elif request_path.startswith("/browser/"):
        return "BROWSER"
    elif request_path.startswith("/git/"):
        return "GIT"
    elif request_path.startswith(("/exec", "/process", "/sessions")):
        return "EXEC"
    else:
        return "UNKNOWN"


def create_scoped_authenticator(required_scope: str):
    """Create a scoped authenticator for a specific tool group."""

    def authenticate_scope(credentials: HTTPAuthorizationCredentials = Depends(security)) -> bool:
        """Validate the scoped bearer token."""
        if not SCOPED_TOKENS:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Bridge server not properly initialized",
            )

        expected_token = SCOPED_TOKENS.get(required_scope)
        if not expected_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"No token configured for scope {required_scope}",
            )

        if not hmac.compare_digest(credentials.credentials, expected_token):
            logger.warning("Invalid auth token attempted for scope %s", required_scope)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token"
            )

        logger.debug("Authenticated request for scope %s", required_scope)
        return True

    return authenticate_scope


# Create scoped authenticators for each tool group
authenticate_notes = create_scoped_authenticator("NOTES")
authenticate_reminders = create_scoped_authenticator("REMINDERS")
authenticate_things = create_scoped_authenticator("THINGS")
authenticate_imessage = create_scoped_authenticator("IMESSAGE")
authenticate_browser = create_scoped_authenticator("BROWSER")
authenticate_git = create_scoped_authenticator("GIT")
authenticate_exec = create_scoped_authenticator("EXEC")


def run_command(cmd: list[str], timeout: int = 30, cwd: str | None = None) -> BridgeResponse:
    """Execute a CLI command safely using subprocess.

    Args:
        cmd: Command as list of strings (never shell=True)
        timeout: Timeout in seconds
        cwd: Working directory for command execution

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
            cwd=cwd,
            check=False,  # Don't raise on non-zero exit codes
        )

        if result.returncode == 0:
            logger.info("Command succeeded (execution_id=%s)", execution_id)
            stdout = result.stdout.strip()
            if len(stdout) > MAX_OUTPUT_BYTES:
                stdout = stdout[:MAX_OUTPUT_BYTES] + "\n...[output truncated]"
            return BridgeResponse(ok=True, output=stdout, execution_id=execution_id)
        else:
            logger.warning(
                "Command failed with exit code %d (execution_id=%s): %s",
                result.returncode,
                execution_id,
                result.stderr.strip(),
            )
            return BridgeResponse(
                ok=False,
                error=f"Command failed with exit code {result.returncode}: {result.stderr.strip()}",
                execution_id=execution_id,
            )

    except subprocess.TimeoutExpired:
        logger.error("Command timed out after %ds (execution_id=%s)", timeout, execution_id)
        return BridgeResponse(
            ok=False, error=f"Command timed out after {timeout} seconds", execution_id=execution_id
        )
    except FileNotFoundError:
        logger.error("Command not found: %s (execution_id=%s)", cmd[0], execution_id)
        return BridgeResponse(
            ok=False, error=f"Command not found: {cmd[0]}", execution_id=execution_id
        )
    except Exception as e:
        logger.error("Unexpected error running command (execution_id=%s): %s", execution_id, e)
        return BridgeResponse(
            ok=False, error=f"Unexpected error: {str(e)}", execution_id=execution_id
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan manager."""
    global SCOPED_TOKENS

    # Generate scoped auth tokens
    scopes = ["NOTES", "REMINDERS", "THINGS", "IMESSAGE", "BROWSER", "GIT", "EXEC"]

    for scope in scopes:
        env_var = f"BRIDGE_TOKEN_{scope}"
        token = os.environ.get(env_var)
        if not token:
            token = secrets.token_urlsafe(32)
            logger.info("Generated %s bridge token (prefix): %s...", scope.lower(), token[:8])
            logger.warning(
                "Consider setting %s environment variable to persist this token", env_var
            )
        else:
            logger.info("Using configured %s bridge token", scope.lower())

        SCOPED_TOKENS[scope] = token

    logger.info(
        "Bridge server ready on %s:%s with %d scoped tokens",
        os.environ.get("BRIDGE_HOST", "127.0.0.1"),
        os.environ.get("BRIDGE_PORT", "8099"),
        len(SCOPED_TOKENS),
    )

    # Initialize BrowserRelay if playwright is available
    browser_relay = None
    try:
        from bridge.browser import BrowserRelay

        blocked_str = os.environ.get("BROWSER_BLOCKED_DOMAINS", "")
        blocked_domains = [d.strip() for d in blocked_str.split(",") if d.strip()]
        browser_relay = BrowserRelay(
            max_sessions=int(os.environ.get("BROWSER_MAX_SESSIONS", "3")),
            session_timeout_minutes=int(os.environ.get("BROWSER_SESSION_TIMEOUT", "10")),
            blocked_domains=blocked_domains,
            container_memory=os.environ.get("BROWSER_CONTAINER_MEMORY", "1024m"),
            container_shm_size=os.environ.get("BROWSER_CONTAINER_SHM_SIZE", "256m"),
            container_tmpfs_size=os.environ.get("BROWSER_CONTAINER_TMPFS_SIZE", "128M"),
            navigate_timeout_ms=int(os.environ.get("BROWSER_NAVIGATE_TIMEOUT_MS", "30000")),
            snapshot_timeout_ms=int(os.environ.get("BROWSER_SNAPSHOT_TIMEOUT_MS", "15000")),
            block_heavy_resources=os.environ.get("BROWSER_BLOCK_HEAVY_RESOURCES", "true").lower()
            in ("true", "1", "yes"),
        )
        await browser_relay.start()
        app.state.browser_relay = browser_relay
        logger.info("BrowserRelay initialized")
    except ImportError:
        logger.info("Playwright not installed — browser endpoints disabled")
        app.state.browser_relay = None

    # Initialize ProcessManager
    allowed_workdirs_str = os.environ.get("EXEC_ALLOWED_WORKDIRS", "")
    allowed_workdirs = [d.strip() for d in allowed_workdirs_str.split(",") if d.strip()] or None
    process_manager = ProcessManager(
        max_sessions=int(os.environ.get("EXEC_MAX_SESSIONS", "10")),
        max_age_hours=int(os.environ.get("EXEC_MAX_AGE_HOURS", "4")),
        allowed_workdirs=allowed_workdirs,
    )
    app.state.process_manager = process_manager
    logger.info("ProcessManager initialized")

    yield

    # Shut down ProcessManager
    process_manager.shutdown()

    # Shut down BrowserRelay
    if browser_relay:
        await browser_relay.stop()

    logger.info("Bridge server shutting down")


# Create FastAPI app
app = FastAPI(
    title="Creel Host Bridge",
    description="HTTP bridge for macOS-native tools",
    version="1.0.0",
    lifespan=lifespan,
)


# Health check endpoint (no auth required)
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "creel-bridge"}


# Notes endpoints (via memo CLI)
@app.post("/notes/list", response_model=BridgeResponse)
async def notes_list(
    request: NotesListRequest = NotesListRequest(), _: bool = Depends(authenticate_notes)
) -> BridgeResponse:
    """List notes via memo CLI."""
    cmd = ["memo", "notes"]
    if request.folder:
        cmd.extend(["-f", request.folder])

    return run_command(cmd)


@app.post("/notes/search", response_model=BridgeResponse)
async def notes_search(
    request: NotesSearchRequest, _: bool = Depends(authenticate_notes)
) -> BridgeResponse:
    """Search notes via memo CLI."""
    cmd = ["memo", "notes", "-s", request.query]
    return run_command(cmd)


@app.post("/notes/create", response_model=BridgeResponse)
async def notes_create(
    request: NotesCreateRequest, _: bool = Depends(authenticate_notes)
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
    _: bool = Depends(authenticate_reminders),
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


@app.post("/reminders/lists", response_model=BridgeResponse)
async def reminders_lists(_: bool = Depends(authenticate_reminders)) -> BridgeResponse:
    """List available reminder lists via remindctl CLI."""
    return run_command(["remindctl", "list"])


@app.post("/reminders/add", response_model=BridgeResponse)
async def reminders_add(
    request: RemindersAddRequest, _: bool = Depends(authenticate_reminders)
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
    request: RemindersCompleteRequest, _: bool = Depends(authenticate_reminders)
) -> BridgeResponse:
    """Complete a reminder via remindctl CLI."""
    cmd = ["remindctl", "complete", request.id]
    return run_command(cmd)


# Things 3 endpoints (via things CLI)
@app.post("/things/inbox", response_model=BridgeResponse)
async def things_inbox(
    request: ThingsInboxRequest = ThingsInboxRequest(), _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Get Things 3 inbox via things CLI."""
    cmd = ["things", "inbox", "--limit", str(request.limit)]
    return run_command(cmd)


@app.post("/things/today", response_model=BridgeResponse)
async def things_today(_: bool = Depends(authenticate_things)) -> BridgeResponse:
    """Get Things 3 today list via things CLI."""
    cmd = ["things", "today"]
    return run_command(cmd)


@app.post("/things/upcoming", response_model=BridgeResponse)
async def things_upcoming(_: bool = Depends(authenticate_things)) -> BridgeResponse:
    """Get Things 3 upcoming list via things CLI."""
    cmd = ["things", "upcoming"]
    return run_command(cmd)


@app.post("/things/search", response_model=BridgeResponse)
async def things_search(
    request: ThingsSearchRequest, _: bool = Depends(authenticate_things)
) -> BridgeResponse:
    """Search Things 3 via things CLI."""
    cmd = ["things", "search", request.query]
    return run_command(cmd)


@app.post("/things/projects", response_model=BridgeResponse)
async def things_projects(_: bool = Depends(authenticate_things)) -> BridgeResponse:
    """Get Things 3 projects via things CLI."""
    cmd = ["things", "projects"]
    return run_command(cmd)


@app.post("/things/add", response_model=BridgeResponse)
async def things_add(
    request: ThingsAddRequest, _: bool = Depends(authenticate_things)
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
    request: ThingsUpdateRequest, _: bool = Depends(authenticate_things)
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
    _: bool = Depends(authenticate_imessage),
) -> BridgeResponse:
    """Get recent iMessages via imsg CLI."""
    if not _check_imsg_available():
        return BridgeResponse(ok=False, error="imsg CLI not found at /opt/homebrew/bin/imsg")

    cmd = ["/opt/homebrew/bin/imsg", "recent", "--limit", str(request.limit)]
    return run_command(cmd)


@app.post("/imessage/send", response_model=BridgeResponse)
async def imessage_send(
    request: IMessageSendRequest, _: bool = Depends(authenticate_imessage)
) -> BridgeResponse:
    """Send iMessage via imsg CLI."""
    if not _check_imsg_available():
        return BridgeResponse(ok=False, error="imsg CLI not found at /opt/homebrew/bin/imsg")

    cmd = ["/opt/homebrew/bin/imsg", "send", "--to", request.to, "--text", request.text]
    return run_command(cmd)


@app.post("/imessage/chats", response_model=BridgeResponse)
async def imessage_chats(_: bool = Depends(authenticate_imessage)) -> BridgeResponse:
    """Get iMessage chats via imsg CLI."""
    if not _check_imsg_available():
        return BridgeResponse(ok=False, error="imsg CLI not found at /opt/homebrew/bin/imsg")

    cmd = ["/opt/homebrew/bin/imsg", "chats"]
    return run_command(cmd)


# Browser endpoints
def _get_relay():
    """Get the BrowserRelay from app state, raising if unavailable."""
    relay = getattr(app.state, "browser_relay", None)
    if relay is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Browser relay not available (playwright not installed?)",
        )
    return relay


@app.post("/browser/connect")
async def browser_connect(
    body: BrowserConnectRequest,
    _: bool = Depends(authenticate_browser),
):
    """Create a new browser session."""
    relay = _get_relay()
    try:
        if body.mode == "relay":
            if not body.cdp_url:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="cdp_url required for relay mode",
                )
            session_id = await relay.connect_relay(body.cdp_url)
        elif body.mode == "managed":
            session_id = await relay.create_managed(headless=body.headless)
        elif body.mode == "native":
            session_id = await relay.create_native(headless=body.headless)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown mode: {body.mode}. Use 'managed', 'relay', or 'native'.",
            )
        return {"ok": True, "session_id": session_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Browser connect error: %s", e)
        return {"ok": False, "error": str(e)}


@app.post("/browser/navigate")
async def browser_navigate(
    body: BrowserNavigateRequest,
    _: bool = Depends(authenticate_browser),
):
    """Navigate to a URL and return page content."""
    relay = _get_relay()
    try:
        result = await relay.navigate(body.session_id, body.url)
        return {"ok": True, **result}
    except SessionDead as e:
        logger.warning("Browser session dead: %s", e)
        return {"ok": False, "error": str(e), "session_dead": True}
    except Exception as e:
        logger.warning("Browser endpoint error: %s", e)
        return {"ok": False, "error": str(e)}


@app.post("/browser/content")
async def browser_content(
    body: BrowserContentRequest,
    _: bool = Depends(authenticate_browser),
):
    """Get current page content as accessibility tree."""
    relay = _get_relay()
    try:
        result = await relay.get_content(body.session_id, body.selector)
        return {"ok": True, **result}
    except SessionDead as e:
        logger.warning("Browser session dead: %s", e)
        return {"ok": False, "error": str(e), "session_dead": True}
    except Exception as e:
        logger.warning("Browser endpoint error: %s", e)
        return {"ok": False, "error": str(e)}


@app.post("/browser/click")
async def browser_click(
    body: BrowserClickRequest,
    _: bool = Depends(authenticate_browser),
):
    """Click an element on the page."""
    relay = _get_relay()
    try:
        result = await relay.click(body.session_id, body.selector)
        return {"ok": True, **result}
    except SessionDead as e:
        logger.warning("Browser session dead: %s", e)
        return {"ok": False, "error": str(e), "session_dead": True}
    except Exception as e:
        logger.warning("Browser endpoint error: %s", e)
        return {"ok": False, "error": str(e)}


@app.post("/browser/type")
async def browser_type(
    body: BrowserTypeRequest,
    _: bool = Depends(authenticate_browser),
):
    """Type text into an input element."""
    relay = _get_relay()
    try:
        result = await relay.type_text(body.session_id, body.selector, body.text)
        return {"ok": True, **result}
    except SessionDead as e:
        logger.warning("Browser session dead: %s", e)
        return {"ok": False, "error": str(e), "session_dead": True}
    except Exception as e:
        logger.warning("Browser endpoint error: %s", e)
        return {"ok": False, "error": str(e)}


@app.post("/browser/screenshot")
async def browser_screenshot(
    body: BrowserScreenshotRequest,
    _: bool = Depends(authenticate_browser),
):
    """Take a screenshot of the current page."""
    relay = _get_relay()
    try:
        result = await relay.screenshot(body.session_id, body.full_page)
        return {"ok": True, **result}
    except SessionDead as e:
        logger.warning("Browser session dead: %s", e)
        return {"ok": False, "error": str(e), "session_dead": True}
    except Exception as e:
        logger.warning("Browser endpoint error: %s", e)
        return {"ok": False, "error": str(e)}


@app.post("/browser/links")
async def browser_links(
    body: BrowserLinksRequest,
    _: bool = Depends(authenticate_browser),
):
    """Get all links on the current page."""
    relay = _get_relay()
    try:
        links = await relay.get_links(body.session_id)
        return {"ok": True, "links": links}
    except SessionDead as e:
        logger.warning("Browser session dead: %s", e)
        return {"ok": False, "error": str(e), "session_dead": True}
    except Exception as e:
        logger.warning("Browser endpoint error: %s", e)
        return {"ok": False, "error": str(e)}


@app.post("/browser/close")
async def browser_close(
    body: BrowserCloseRequest,
    _: bool = Depends(authenticate_browser),
):
    """Close a browser session."""
    relay = _get_relay()
    try:
        await relay.close_session(body.session_id)
        return {"ok": True}
    except Exception as e:
        logger.warning("Browser endpoint error: %s", e)
        return {"ok": False, "error": str(e)}


@app.get("/browser/sessions")
async def browser_sessions(
    _: bool = Depends(authenticate_browser),
):
    """List active browser sessions."""
    relay = _get_relay()
    return {"ok": True, "sessions": relay.list_sessions()}


# Git endpoints
GIT_REPO_DIR = os.environ.get("GIT_REPO_DIR", os.getcwd())
logger.info("Git repo directory: %s", GIT_REPO_DIR)


@app.post("/git/status", response_model=BridgeResponse)
async def git_status(
    request: GitStatusRequest = GitStatusRequest(),
    _: bool = Depends(authenticate_git),
) -> BridgeResponse:
    """Get git status."""
    cmd = ["git", "status"]
    if request.short:
        cmd.append("--short")
    return run_command(cmd, cwd=GIT_REPO_DIR)


@app.post("/git/diff", response_model=BridgeResponse)
async def git_diff(
    request: GitDiffRequest = GitDiffRequest(),
    _: bool = Depends(authenticate_git),
) -> BridgeResponse:
    """Get git diff."""
    cmd = ["git", "diff"]
    if request.cached:
        cmd.append("--cached")
    if request.path:
        cmd.extend(["--", request.path])
    return run_command(cmd, cwd=GIT_REPO_DIR)


@app.post("/git/log", response_model=BridgeResponse)
async def git_log(
    request: GitLogRequest = GitLogRequest(),
    _: bool = Depends(authenticate_git),
) -> BridgeResponse:
    """Get git log."""
    cmd = ["git", "log", f"--max-count={request.max_count}"]
    if request.oneline:
        cmd.append("--oneline")
    return run_command(cmd, cwd=GIT_REPO_DIR)


@app.post("/git/commit", response_model=BridgeResponse)
async def git_commit(
    request: GitCommitRequest,
    _: bool = Depends(authenticate_git),
) -> BridgeResponse:
    """Create a git commit."""
    cmd = ["git", "commit", "-m", request.message]
    if request.all:
        cmd.insert(2, "-a")
    return run_command(cmd, cwd=GIT_REPO_DIR)


@app.post("/git/branch", response_model=BridgeResponse)
async def git_branch(
    request: GitBranchRequest = GitBranchRequest(),
    _: bool = Depends(authenticate_git),
) -> BridgeResponse:
    """List or manage git branches."""
    cmd = ["git", "branch"]
    if request.list_all:
        cmd.append("-a")
    elif request.name:
        if request.delete:
            cmd.extend(["-d", "--", request.name])
        else:
            cmd.extend(["--", request.name])
    return run_command(cmd, cwd=GIT_REPO_DIR)


@app.post("/git/push", response_model=BridgeResponse)
async def git_push(
    request: GitPushRequest = GitPushRequest(),
    _: bool = Depends(authenticate_git),
) -> BridgeResponse:
    """Push to remote."""
    cmd = ["git", "push"]
    if request.set_upstream:
        cmd.append("-u")
    cmd.append(request.remote)
    if request.branch:
        cmd.append(request.branch)
    return run_command(cmd, cwd=GIT_REPO_DIR, timeout=60)


def _get_process_manager() -> ProcessManager:
    """Get the ProcessManager from app state."""
    pm = getattr(app.state, "process_manager", None)
    if pm is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ProcessManager not initialized",
        )
    return pm


# Exec endpoints
@app.post("/exec")
async def exec_command(
    body: ExecRequest,
    _: bool = Depends(authenticate_exec),
):
    """Spawn a command on the host."""
    pm = _get_process_manager()
    try:
        result = pm.spawn(
            command=body.command,
            workdir=body.workdir,
            background=body.background,
            timeout=body.timeout,
            env=body.env,
        )
        return {"ok": True, **result}
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.warning("Exec error: %s", e)
        return {"ok": False, "error": str(e)}


@app.post("/process")
async def process_action(
    body: ProcessActionRequest,
    _: bool = Depends(authenticate_exec),
):
    """Manage a running process."""
    pm = _get_process_manager()
    try:
        if body.action == "poll":
            result = pm.poll(body.session_id)
            return {"ok": True, **result}

        elif body.action == "log":
            lines = pm.log(body.session_id, limit=body.limit, offset=body.offset)
            return {"ok": True, "session_id": body.session_id, "lines": lines}

        elif body.action == "write":
            if body.data is None:
                return {"ok": False, "error": "data is required for write action"}
            result = pm.write(body.session_id, body.data)
            return {"ok": True, **result}

        elif body.action == "kill":
            result = pm.kill(body.session_id)
            return {"ok": True, **result}

        else:
            return {"ok": False, "error": f"Unknown action: {body.action}"}

    except KeyError as e:
        return {"ok": False, "error": str(e)}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.warning("Process action error: %s", e)
        return {"ok": False, "error": str(e)}


@app.get("/sessions")
async def list_sessions(
    _: bool = Depends(authenticate_exec),
):
    """List all active process sessions."""
    pm = _get_process_manager()
    return {"ok": True, "sessions": pm.list_sessions()}


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
        level=args.log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
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
        access_log=True,
    )


if __name__ == "__main__":
    main()
