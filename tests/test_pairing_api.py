"""Tests for the device pairing daemon API endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from creel.daemon.api_pairing import create_pairing_routes
from creel.pairing import PairingManager, _generate_totp_code


@pytest.fixture
def pairing_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pairing"
    d.mkdir()
    return d


@pytest.fixture
def manager(pairing_dir: Path) -> PairingManager:
    return PairingManager(pairing_dir)


@pytest.fixture
def app(manager: PairingManager) -> FastAPI:
    app = FastAPI()
    app.state.pairing_manager = manager
    app.state.dashboard_token = "test-token"
    http_router, ws_router = create_pairing_routes(manager)
    app.include_router(http_router)
    app.include_router(ws_router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestGenerateEndpoint:
    def test_generate_default_timeout(self, client: TestClient) -> None:
        resp = client.post("/api/pairing/generate")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert "pairing_code" in data
        assert len(data["pairing_code"]) == 8
        assert "totp_secret" in data
        assert "expires_at" in data

    def test_generate_custom_timeout(self, client: TestClient) -> None:
        resp = client.post("/api/pairing/generate?timeout_seconds=120")
        assert resp.status_code == 200
        data = resp.json()
        assert data["expires_at"] > 0


class TestCompleteEndpoint:
    def test_complete_success(self, client: TestClient, manager: PairingManager) -> None:
        # Generate a pairing session
        gen_resp = client.post("/api/pairing/generate")
        session_data = gen_resp.json()

        totp_code = _generate_totp_code(session_data["totp_secret"])

        resp = client.post(
            "/api/pairing/complete",
            json={
                "pairing_code": session_data["pairing_code"],
                "totp_code": totp_code,
                "device_name": "TestPhone",
                "device_type": "phone",
                "capabilities": ["push_notifications", "camera"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "TestPhone"
        assert data["device_type"] == "phone"
        assert "push_notifications" in data["capabilities"]

    def test_complete_bad_code(self, client: TestClient) -> None:
        resp = client.post(
            "/api/pairing/complete",
            json={
                "pairing_code": "DEADBEEF",
                "totp_code": "123456",
                "device_name": "Phone",
            },
        )
        assert resp.status_code == 404

    def test_complete_bad_totp(self, client: TestClient) -> None:
        gen_resp = client.post("/api/pairing/generate")
        session_data = gen_resp.json()

        resp = client.post(
            "/api/pairing/complete",
            json={
                "pairing_code": session_data["pairing_code"],
                "totp_code": "000000",
                "device_name": "Phone",
            },
        )
        assert resp.status_code == 403


class TestDevicesEndpoint:
    def test_list_empty(self, client: TestClient) -> None:
        resp = client.get("/api/pairing/devices")
        assert resp.status_code == 200
        assert resp.json()["devices"] == []

    def test_list_after_pairing(self, client: TestClient) -> None:
        # Pair a device
        gen_resp = client.post("/api/pairing/generate")
        session_data = gen_resp.json()
        totp_code = _generate_totp_code(session_data["totp_secret"])
        client.post(
            "/api/pairing/complete",
            json={
                "pairing_code": session_data["pairing_code"],
                "totp_code": totp_code,
                "device_name": "Phone",
            },
        )

        resp = client.get("/api/pairing/devices")
        assert resp.status_code == 200
        devices = resp.json()["devices"]
        assert len(devices) == 1
        assert devices[0]["name"] == "Phone"

    def test_get_device(self, client: TestClient) -> None:
        gen_resp = client.post("/api/pairing/generate")
        session_data = gen_resp.json()
        totp_code = _generate_totp_code(session_data["totp_secret"])
        pair_resp = client.post(
            "/api/pairing/complete",
            json={
                "pairing_code": session_data["pairing_code"],
                "totp_code": totp_code,
                "device_name": "Phone",
            },
        )
        device_id = pair_resp.json()["id"]

        resp = client.get(f"/api/pairing/devices/{device_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Phone"

    def test_get_device_not_found(self, client: TestClient) -> None:
        resp = client.get(f"/api/pairing/devices/{'ab' * 16}")
        assert resp.status_code == 404

    def test_delete_device(self, client: TestClient) -> None:
        gen_resp = client.post("/api/pairing/generate")
        session_data = gen_resp.json()
        totp_code = _generate_totp_code(session_data["totp_secret"])
        pair_resp = client.post(
            "/api/pairing/complete",
            json={
                "pairing_code": session_data["pairing_code"],
                "totp_code": totp_code,
                "device_name": "Phone",
            },
        )
        device_id = pair_resp.json()["id"]

        resp = client.delete(f"/api/pairing/devices/{device_id}")
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

        # Verify it's gone
        resp = client.get(f"/api/pairing/devices/{device_id}")
        assert resp.status_code == 404

    def test_delete_device_not_found(self, client: TestClient) -> None:
        resp = client.delete(f"/api/pairing/devices/{'ab' * 16}")
        assert resp.status_code == 404


class TestWebSocket:
    def test_ws_pairing_session_not_found(self, client: TestClient) -> None:
        with client.websocket_connect(f"/ws/pairing/{'ab' * 16}?token=test-token") as ws:
            data = ws.receive_json()
            assert data["error"] == "session_not_found"

    def test_ws_pairing_tracks_status(self, client: TestClient, manager: PairingManager) -> None:
        session = manager.generate_pairing(timeout_seconds=60)

        with client.websocket_connect(f"/ws/pairing/{session.session_id}?token=test-token") as ws:
            data = ws.receive_json()
            assert data["status"] == "pending"
            assert data["session_id"] == session.session_id

    def test_ws_unauthorized(self, client: TestClient) -> None:
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/pairing/{'ab' * 16}?token=wrong") as ws:
                ws.receive_json()
