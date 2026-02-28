"""HTTP API for daemon clients."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from taskrunner.daemon.api_auth import ensure_dashboard_token, require_dashboard_token
from taskrunner.daemon.api_config import router as config_router
from taskrunner.daemon.api_cron import router as cron_router
from taskrunner.daemon.api_dashboard import router as dashboard_router
from taskrunner.daemon.api_files import router as files_router
from taskrunner.daemon.api_logs import router as logs_router
from taskrunner.daemon.api_logs import ws_router as logs_ws_router
from taskrunner.daemon.api_tasks import router as tasks_router
from taskrunner.daemon.contracts import (
    DaemonStatusResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionHistoryResponse,
    SessionRequest,
    SessionSummary,
    StreamEvent,
)
from taskrunner.daemon.service import DaemonService

logger = logging.getLogger(__name__)

_DASHBOARD_STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard_static"


def _mount_webhook_routes(app: FastAPI, service: DaemonService) -> None:
    """Mount webhook routes from any channels that provide them.

    NOTE: In deferred-init mode this is called from the background init thread.
    This is safe because the 503 middleware blocks all non-health requests until
    ``ready`` is set (which happens after this function returns).
    """
    for name, channel in service.get_channels().items():
        routes = channel.get_webhook_routes()
        if not routes:
            continue
        for route in routes:
            method = route["method"].upper()
            path = route["path"]
            handler = route["handler"]
            if method == "GET":
                app.get(path)(handler)
            elif method == "POST":
                app.post(path)(handler)
            else:
                app.api_route(path, methods=[method])(handler)


def create_daemon_app(
    service: DaemonService | None = None,
    *,
    init_factory: Callable[[], DaemonService] | None = None,
) -> FastAPI:
    """Create a FastAPI app bound to a daemon service instance.

    Two modes of operation:

    1. **Immediate** (*service* provided): the app is ready to serve all
       endpoints as soon as the lifespan starts.  This is the path used by
       tests and simple callers.

    2. **Deferred** (*init_factory* provided): the socket becomes available
       immediately (``/health`` returns ``{"status": "starting"}``), while
       heavy initialization runs in a background thread.  Once the factory
       returns, the app transitions to fully ready.
    """
    ready = threading.Event()
    failed = threading.Event()
    init_error: list[BaseException] = []  # mutable container for thread-safe error capture

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.dashboard_token = ensure_dashboard_token()
        init_thread: threading.Thread | None = None
        if service is not None:
            app.state.service = service
            _mount_webhook_routes(app, service)
            ready.set()
        elif init_factory is not None:

            def _init() -> None:
                try:
                    svc = init_factory()
                    app.state.service = svc
                    _mount_webhook_routes(app, svc)
                    ready.set()
                except Exception as exc:
                    logger.exception("Deferred daemon initialization failed")
                    init_error.append(exc)
                    failed.set()

            init_thread = threading.Thread(target=_init, daemon=True, name="creel-deferred-init")
            init_thread.start()

        yield

        if init_thread is not None:
            # Best-effort wait; the thread is a daemon so it won't block exit.
            init_thread.join(timeout=5.0)
        svc = getattr(app.state, "service", None)
        if svc is not None:
            svc.shutdown()

    app = FastAPI(
        title="Creel Daemon API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # NOTE: Starlette HTTP middleware does not intercept WebSocket upgrades.
    # If WebSocket routes are added, they need their own readiness guard.
    @app.middleware("http")
    async def _require_ready(request: Request, call_next):
        if request.url.path != "/health" and not ready.is_set():
            detail = "Service initialization failed" if failed.is_set() else "Service is starting"
            return JSONResponse(
                status_code=503,
                content={"detail": detail},
            )
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        if failed.is_set():
            result: dict[str, str] = {"status": "error", "service": "creel-daemon"}
            if init_error:
                result["error"] = str(init_error[0])
            return result
        elif ready.is_set():
            status = "ok"
        else:
            status = "starting"
        return {"status": status, "service": "creel-daemon"}

    @app.get("/v1/status", response_model=DaemonStatusResponse)
    async def status() -> DaemonStatusResponse:
        return DaemonStatusResponse(**app.state.service.status())

    @app.post("/v1/messages", response_model=SendMessageResponse)
    async def send_message(request: SendMessageRequest) -> SendMessageResponse:
        svc = app.state.service
        try:
            if request.session_id:
                await asyncio.to_thread(svc.resume_session, request.sender_id, request.session_id)
            text = await asyncio.to_thread(
                svc.send_message,
                request.sender_id,
                request.text,
                auto_approve=request.auto_approve,
            )
            session_id = await asyncio.to_thread(svc.get_active_session_id, request.sender_id)
            return SendMessageResponse(
                sender_id=request.sender_id,
                text=text,
                session_id=session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/messages/stream")
    async def stream_message(request: SendMessageRequest) -> StreamingResponse:
        svc = app.state.service

        async def _iter_sse():
            # Run the blocking generator in a thread and yield events
            q: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            def _produce():
                try:
                    for raw_event in svc.stream_message(
                        sender_id=request.sender_id,
                        text=request.text,
                        session_id=request.session_id,
                        auto_approve=request.auto_approve,
                    ):
                        asyncio.run_coroutine_threadsafe(q.put(raw_event), loop).result()
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(sentinel), loop).result()

            loop = asyncio.get_event_loop()
            asyncio.get_event_loop().run_in_executor(None, _produce)

            while True:
                item = await q.get()
                if item is sentinel:
                    break
                event = StreamEvent(**item).model_dump()
                event_type = event["type"]
                payload = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\n"
                yield f"data: {payload}\n\n"

        return StreamingResponse(
            _iter_sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @app.get("/v1/sessions", response_model=list[SessionSummary])
    async def list_sessions(sender_id: str = Query(..., min_length=1)) -> list[SessionSummary]:
        rows = await asyncio.to_thread(app.state.service.list_sessions, sender_id)
        return [SessionSummary(sender_id=sender_id, **row) for row in rows]

    @app.get("/v1/sessions/active", response_model=SessionSummary)
    async def active_session(sender_id: str = Query(..., min_length=1)) -> SessionSummary:
        row = await asyncio.to_thread(app.state.service.get_active_session, sender_id)
        return SessionSummary(**row)

    @app.post("/v1/sessions/new", response_model=SessionSummary)
    async def new_session(request: SessionRequest) -> SessionSummary:
        row = await asyncio.to_thread(app.state.service.new_session, request.sender_id)
        return SessionSummary(**row)

    @app.post("/v1/sessions/{session_id}/resume", response_model=SessionSummary)
    async def resume_session(session_id: str, request: SessionRequest) -> SessionSummary:
        try:
            row = await asyncio.to_thread(
                app.state.service.resume_session, request.sender_id, session_id
            )
            return SessionSummary(**row)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/sessions/{session_id}/history", response_model=SessionHistoryResponse)
    async def session_history(
        session_id: str,
        sender_id: str = Query(..., min_length=1),
        limit: int = Query(100, ge=1, le=1000),
    ) -> SessionHistoryResponse:
        try:
            messages = await asyncio.to_thread(
                app.state.service.get_history,
                sender_id=sender_id,
                session_id=session_id,
                limit=limit,
            )
            return SessionHistoryResponse(
                sender_id=sender_id,
                session_id=session_id,
                messages=messages,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Mount dashboard API routes (/api/*) — all require auth
    _auth_deps = [Depends(require_dashboard_token)]
    app.include_router(dashboard_router, dependencies=_auth_deps)
    app.include_router(tasks_router, dependencies=_auth_deps)
    app.include_router(cron_router, dependencies=_auth_deps)
    app.include_router(files_router, dependencies=_auth_deps)
    app.include_router(config_router, dependencies=_auth_deps)
    app.include_router(logs_router, dependencies=_auth_deps)
    app.include_router(logs_ws_router)  # WebSocket — handles its own auth

    # Serve the dashboard SPA if the built static files are present
    _mount_dashboard(app)

    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Mount dashboard static files and SPA fallback if the build directory exists."""
    if not _DASHBOARD_STATIC_DIR.is_dir():
        return

    index_html = _DASHBOARD_STATIC_DIR / "index.html"
    if not index_html.is_file():
        return

    # Mount static assets (JS/CSS/images) so they are served directly
    if (_DASHBOARD_STATIC_DIR / "assets").is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_DASHBOARD_STATIC_DIR / "assets")),
            name="dashboard-assets",
        )

    # SPA fallback: any GET request that isn't an API/v1/health route
    # gets index.html so client-side routing works.
    backend_prefixes = ("api/", "v1/", "health/", "docs/", "redoc/")
    backend_exact = {"api", "v1", "health", "docs", "redoc", "openapi.json"}

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str) -> FileResponse:
        # Preserve proper 404 semantics for backend/API namespaces.
        if full_path in backend_exact or full_path.startswith(backend_prefixes):
            raise HTTPException(status_code=404, detail="Not Found")

        # Serve actual static files if they exist (e.g. favicon.ico, manifest.json)
        static_file = _DASHBOARD_STATIC_DIR / full_path
        if (
            full_path
            and static_file.is_file()
            and _DASHBOARD_STATIC_DIR in static_file.resolve().parents
        ):
            return FileResponse(str(static_file))
        return FileResponse(str(index_html))
