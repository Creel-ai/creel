"""Device pairing HTTP + WebSocket API endpoints."""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from creel.pairing import (
    DeviceCapability,
    DeviceType,
    PairingManager,
    PairingStatus,
)

logger = logging.getLogger(__name__)


# --- Request / Response models ---


class GeneratePairingResponse(BaseModel):
    session_id: str
    pairing_code: str
    totp_secret: str
    expires_at: float


class CompletePairingRequest(BaseModel):
    pairing_code: str = Field(min_length=1, max_length=16)
    totp_code: str = Field(min_length=6, max_length=6)
    device_name: str = Field(min_length=1, max_length=128)
    device_type: DeviceType = DeviceType.OTHER
    capabilities: list[DeviceCapability] = Field(default_factory=list)


class DeviceResponse(BaseModel):
    id: str
    name: str
    device_type: str
    capabilities: list[str]
    last_seen: float
    paired_at: float


class DeviceListResponse(BaseModel):
    devices: list[DeviceResponse]


def create_pairing_routes(manager: PairingManager) -> tuple[APIRouter, APIRouter]:
    """Create pairing API routers bound to a ``PairingManager`` instance.

    Returns ``(http_router, ws_router)``.
    """
    http = APIRouter(prefix="/api/pairing", tags=["pairing"])
    ws = APIRouter(tags=["pairing"])

    @http.post("/generate", response_model=GeneratePairingResponse)
    async def api_generate_pairing(
        timeout_seconds: int = 300,
    ) -> GeneratePairingResponse:
        session = await asyncio.to_thread(manager.generate_pairing, timeout_seconds)
        return GeneratePairingResponse(
            session_id=session.session_id,
            pairing_code=session.pairing_code,
            totp_secret=session.totp_secret,
            expires_at=session.expires_at,
        )

    @http.post("/complete", response_model=DeviceResponse)
    async def api_complete_pairing(req: CompletePairingRequest) -> DeviceResponse:
        # Look up session by pairing code
        session = await asyncio.to_thread(manager.validate_pairing_code, req.pairing_code)
        if session is None:
            raise HTTPException(status_code=404, detail="Invalid or expired pairing code")

        device = await asyncio.to_thread(
            manager.complete_pairing,
            session.session_id,
            req.totp_code,
            req.device_name,
            req.device_type.value,
            [c.value for c in req.capabilities],
        )
        if device is None:
            raise HTTPException(status_code=403, detail="Pairing verification failed")

        return DeviceResponse(
            id=device.id,
            name=device.name,
            device_type=device.device_type,
            capabilities=device.capabilities,
            last_seen=device.last_seen,
            paired_at=device.paired_at,
        )

    @http.get("/devices", response_model=DeviceListResponse)
    async def api_list_devices() -> DeviceListResponse:
        devices = await asyncio.to_thread(manager.list_devices)
        return DeviceListResponse(
            devices=[
                DeviceResponse(
                    id=d.id,
                    name=d.name,
                    device_type=d.device_type,
                    capabilities=d.capabilities,
                    last_seen=d.last_seen,
                    paired_at=d.paired_at,
                )
                for d in devices
            ]
        )

    @http.get("/devices/{device_id}", response_model=DeviceResponse)
    async def api_get_device(device_id: str) -> DeviceResponse:
        try:
            device = await asyncio.to_thread(manager.get_device, device_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid device ID format") from None
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        return DeviceResponse(
            id=device.id,
            name=device.name,
            device_type=device.device_type,
            capabilities=device.capabilities,
            last_seen=device.last_seen,
            paired_at=device.paired_at,
        )

    @http.delete("/devices/{device_id}")
    async def api_remove_device(device_id: str) -> dict[str, bool]:
        try:
            removed = await asyncio.to_thread(manager.remove_device, device_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid device ID format") from None
        if not removed:
            raise HTTPException(status_code=404, detail="Device not found")
        return {"removed": True}

    # --- WebSocket for real-time pairing status ---

    @ws.websocket("/ws/pairing/{session_id}")
    async def ws_pairing(websocket: WebSocket, session_id: str) -> None:
        """Stream pairing session status updates over WebSocket.

        The client connects while waiting for the remote device to complete
        pairing.  The server pushes status changes until the session is
        paired, expired, or rejected.
        """
        # Auth via query-param token (same pattern as logs WebSocket)
        expected_token = getattr(websocket.app.state, "dashboard_token", None)
        client_token = websocket.query_params.get("token")
        if expected_token and (
            not client_token or not hmac.compare_digest(client_token, expected_token)
        ):
            await websocket.close(code=4401, reason="unauthorized")
            return

        await websocket.accept()
        logger.info("WebSocket connected for pairing session %s", session_id)

        try:
            while True:
                session = await asyncio.to_thread(manager._load_session, session_id)
                if session is None:
                    await websocket.send_json({"error": "session_not_found"})
                    break

                status_data = {
                    "session_id": session.session_id,
                    "status": session.status,
                    "device_name": session.device_name,
                    "expires_at": session.expires_at,
                }

                await websocket.send_json(status_data)

                # Terminal states — close the socket
                if session.status in (
                    PairingStatus.PAIRED.value,
                    PairingStatus.EXPIRED.value,
                    PairingStatus.REJECTED.value,
                ):
                    break

                # Check for expiry
                if session.is_expired:
                    await websocket.send_json(
                        {"session_id": session_id, "status": PairingStatus.EXPIRED.value}
                    )
                    break

                await asyncio.sleep(1)
        except WebSocketDisconnect:
            logger.info("WebSocket disconnected for session %s", session_id)

    return http, ws
