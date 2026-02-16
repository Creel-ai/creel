"""HTTP API for daemon clients."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from taskrunner.daemon.contracts import (
    DaemonStatusResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionHistoryResponse,
    SessionRequest,
    SessionSummary,
)
from taskrunner.daemon.service import DaemonService


def create_daemon_app(service: DaemonService) -> FastAPI:
    """Create a FastAPI app bound to a daemon service instance."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service
        yield
        service.shutdown()

    app = FastAPI(
        title="Creel Daemon API",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "creel-daemon"}

    @app.get("/v1/status", response_model=DaemonStatusResponse)
    async def status() -> DaemonStatusResponse:
        return DaemonStatusResponse(**service.status())

    @app.post("/v1/messages", response_model=SendMessageResponse)
    async def send_message(request: SendMessageRequest) -> SendMessageResponse:
        try:
            if request.session_id:
                service.resume_session(request.sender_id, request.session_id)
            text = service.send_message(request.sender_id, request.text)
            return SendMessageResponse(
                sender_id=request.sender_id,
                text=text,
                session_id=service.get_active_session_id(request.sender_id),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/sessions", response_model=list[SessionSummary])
    async def list_sessions(sender_id: str = Query(..., min_length=1)) -> list[SessionSummary]:
        rows = service.list_sessions(sender_id)
        return [SessionSummary(sender_id=sender_id, **row) for row in rows]

    @app.post("/v1/sessions/new", response_model=SessionSummary)
    async def new_session(request: SessionRequest) -> SessionSummary:
        row = service.new_session(request.sender_id)
        return SessionSummary(**row)

    @app.post("/v1/sessions/{session_id}/resume", response_model=SessionSummary)
    async def resume_session(session_id: str, request: SessionRequest) -> SessionSummary:
        try:
            row = service.resume_session(request.sender_id, session_id)
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
            messages = service.get_history(
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

    return app
