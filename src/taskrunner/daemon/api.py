"""HTTP API for daemon clients."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from taskrunner.cron.models import CronJob, Delivery, Payload, Schedule
from taskrunner.daemon.contracts import (
    CreateCronJobRequest,
    CronJobResponse,
    DaemonStatusResponse,
    RunRecordResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionHistoryResponse,
    SessionRequest,
    SessionSummary,
    StreamEvent,
    UpdateCronJobRequest,
)
from taskrunner.daemon.service import DaemonService


def create_daemon_app(service: DaemonService) -> FastAPI:
    """Create a FastAPI app bound to a daemon service instance."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service
        yield
        # Shutdown is handled by the CLI finally block; service.shutdown()
        # is idempotent so calling it here is safe but not required.

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
                await asyncio.to_thread(
                    service.resume_session, request.sender_id, request.session_id
                )
            text = await asyncio.to_thread(
                service.send_message,
                request.sender_id,
                request.text,
                auto_approve=request.auto_approve,
            )
            session_id = await asyncio.to_thread(service.get_active_session_id, request.sender_id)
            return SendMessageResponse(
                sender_id=request.sender_id,
                text=text,
                session_id=session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/messages/stream")
    async def stream_message(request: SendMessageRequest) -> StreamingResponse:
        async def _iter_sse():
            # Run the blocking generator in a thread and yield events
            q: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            def _produce():
                try:
                    for raw_event in service.stream_message(
                        sender_id=request.sender_id,
                        text=request.text,
                        session_id=request.session_id,
                        auto_approve=request.auto_approve,
                    ):
                        asyncio.run_coroutine_threadsafe(q.put(raw_event), loop).result()
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(sentinel), loop).result()

            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _produce)

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
        rows = await asyncio.to_thread(service.list_sessions, sender_id)
        return [SessionSummary(sender_id=sender_id, **row) for row in rows]

    @app.get("/v1/sessions/active", response_model=SessionSummary)
    async def active_session(sender_id: str = Query(..., min_length=1)) -> SessionSummary:
        row = await asyncio.to_thread(service.get_active_session, sender_id)
        return SessionSummary(**row)

    @app.post("/v1/sessions/new", response_model=SessionSummary)
    async def new_session(request: SessionRequest) -> SessionSummary:
        row = await asyncio.to_thread(service.new_session, request.sender_id)
        return SessionSummary(**row)

    @app.post("/v1/sessions/{session_id}/resume", response_model=SessionSummary)
    async def resume_session(session_id: str, request: SessionRequest) -> SessionSummary:
        try:
            row = await asyncio.to_thread(service.resume_session, request.sender_id, session_id)
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
                service.get_history,
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

    # --- Cron job management ---

    def _job_to_response(job: CronJob) -> CronJobResponse:
        return CronJobResponse(**job.model_dump())

    @app.post("/v1/cron/jobs", response_model=CronJobResponse, status_code=201)
    async def create_cron_job(request: CreateCronJobRequest) -> CronJobResponse:
        try:
            delivery = Delivery(**request.delivery) if request.delivery else Delivery(mode="none")
            job = CronJob(
                name=request.name,
                schedule=Schedule(**request.schedule),
                target=request.target,
                payload=Payload(**request.payload),
                delivery=delivery,
                enabled=request.enabled,
            )
            created = await asyncio.to_thread(service.cron_manager.add_job, job)
            return _job_to_response(created)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/cron/jobs", response_model=list[CronJobResponse])
    async def list_cron_jobs() -> list[CronJobResponse]:
        jobs = await asyncio.to_thread(service.cron_manager.list_jobs)
        return [_job_to_response(j) for j in jobs]

    @app.get("/v1/cron/jobs/{job_id}", response_model=CronJobResponse)
    async def get_cron_job(job_id: str) -> CronJobResponse:
        job = await asyncio.to_thread(service.cron_manager.get_job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        return _job_to_response(job)

    @app.put("/v1/cron/jobs/{job_id}", response_model=CronJobResponse)
    async def update_cron_job(job_id: str, request: UpdateCronJobRequest) -> CronJobResponse:
        fields: dict = {}
        if request.name is not None:
            fields["name"] = request.name
        if request.schedule is not None:
            fields["schedule"] = Schedule(**request.schedule)
        if request.payload is not None:
            fields["payload"] = Payload(**request.payload)
        if request.delivery is not None:
            fields["delivery"] = Delivery(**request.delivery)
        if request.enabled is not None:
            fields["enabled"] = request.enabled

        try:
            updated = await asyncio.to_thread(service.cron_manager.update_job, job_id, **fields)
            return _job_to_response(updated)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/v1/cron/jobs/{job_id}", response_model=CronJobResponse)
    async def delete_cron_job(job_id: str) -> CronJobResponse:
        try:
            removed = await asyncio.to_thread(service.cron_manager.remove_job, job_id)
            return _job_to_response(removed)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/cron/jobs/{job_id}/run", status_code=202)
    async def trigger_cron_job(job_id: str) -> dict[str, str]:
        try:
            await asyncio.to_thread(service.cron_manager.trigger_job, job_id)
            return {"status": "triggered", "job_id": job_id}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/cron/jobs/{job_id}/enable", response_model=CronJobResponse)
    async def enable_cron_job(job_id: str) -> CronJobResponse:
        try:
            job = await asyncio.to_thread(service.cron_manager.enable_job, job_id)
            return _job_to_response(job)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/cron/jobs/{job_id}/disable", response_model=CronJobResponse)
    async def disable_cron_job(job_id: str) -> CronJobResponse:
        try:
            job = await asyncio.to_thread(service.cron_manager.disable_job, job_id)
            return _job_to_response(job)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/cron/jobs/{job_id}/runs", response_model=list[RunRecordResponse])
    async def get_cron_job_runs(job_id: str) -> list[RunRecordResponse]:
        # Verify job exists first
        job = await asyncio.to_thread(service.cron_manager.get_job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        runs = await asyncio.to_thread(service.cron_manager.get_runs, job_id)
        return [RunRecordResponse(**r.model_dump()) for r in runs]

    # Mount webhook routes from any channels that provide them
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

    return app
