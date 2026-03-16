"""Tests for the device pairing system."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from creel.pairing import (
    DeviceCapability,
    DeviceType,
    PairedDevice,
    PairingManager,
    PairingSession,
    PairingStatus,
    generate_totp_code,
    verify_totp,
)


@pytest.fixture
def pairing_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pairing"
    d.mkdir()
    return d


@pytest.fixture
def manager(pairing_dir: Path) -> PairingManager:
    return PairingManager(pairing_dir)


# --- TOTP tests ---


class TestTOTP:
    def test_generate_totp_code_deterministic(self) -> None:
        """Same secret and step produce the same code."""
        code1 = generate_totp_code("test-secret", step=1000)
        code2 = generate_totp_code("test-secret", step=1000)
        assert code1 == code2

    def test_generate_totp_code_length(self) -> None:
        code = generate_totp_code("secret", step=42)
        assert len(code) == 6
        assert code.isdigit()

    def test_generate_totp_code_different_steps(self) -> None:
        code1 = generate_totp_code("secret", step=1)
        code2 = generate_totp_code("secret", step=9999)
        assert code1 != code2

    def test_verify_totp_current_step(self) -> None:
        secret = "test-secret-123"
        code = generate_totp_code(secret)
        assert verify_totp(secret, code) is True

    def test_verify_totp_wrong_code(self) -> None:
        assert verify_totp("secret", "000000") is False

    def test_verify_totp_window_tolerance(self) -> None:
        """Codes from adjacent time steps should be accepted."""
        secret = "window-test"
        current_step = int(time.time()) // 30
        # Generate code for previous step
        code = generate_totp_code(secret, step=current_step - 1)
        assert verify_totp(secret, code) is True


# --- PairedDevice tests ---


class TestPairedDevice:
    def test_defaults(self) -> None:
        device = PairedDevice(name="TestPhone")
        assert device.name == "TestPhone"
        assert device.device_type == DeviceType.OTHER.value
        assert len(device.id) == 32  # hex(16)
        assert len(device.auth_token) > 0
        assert device.capabilities == []

    def test_roundtrip_dict(self) -> None:
        device = PairedDevice(
            name="MyPhone",
            device_type=DeviceType.PHONE.value,
            capabilities=[DeviceCapability.PUSH_NOTIFICATIONS.value],
        )
        data = device.to_dict()
        restored = PairedDevice.from_dict(data)
        assert restored.name == device.name
        assert restored.device_type == device.device_type
        assert restored.capabilities == device.capabilities
        assert restored.id == device.id

    def test_from_dict_ignores_extra_keys(self) -> None:
        data = {"name": "Phone", "unknown_field": 42, "id": "ab" * 16}
        device = PairedDevice.from_dict(data)
        assert device.name == "Phone"


# --- PairingSession tests ---


class TestPairingSession:
    def test_is_expired_false(self) -> None:
        session = PairingSession(expires_at=time.time() + 300)
        assert session.is_expired is False

    def test_is_expired_true(self) -> None:
        session = PairingSession(expires_at=time.time() - 1)
        assert session.is_expired is True

    def test_to_dict(self) -> None:
        session = PairingSession(pairing_code="AABBCCDD", totp_secret="secret")
        d = session.to_dict()
        assert d["pairing_code"] == "AABBCCDD"
        assert d["totp_secret"] == "secret"
        assert d["status"] == PairingStatus.PENDING.value


# --- PairingManager tests ---


class TestPairingManager:
    def test_init_creates_directories(self, tmp_path: Path) -> None:
        pairing_dir = tmp_path / "new_pairing"
        PairingManager(pairing_dir)
        assert (pairing_dir / "devices").is_dir()
        assert (pairing_dir / "sessions").is_dir()

    def test_generate_pairing(self, manager: PairingManager) -> None:
        session = manager.generate_pairing(timeout_seconds=60)
        assert len(session.pairing_code) == 8  # 4 hex bytes = 8 chars
        assert session.pairing_code == session.pairing_code.upper()
        assert len(session.totp_secret) > 0
        assert session.status == PairingStatus.PENDING.value
        assert session.expires_at > time.time()

    def test_generate_pairing_persists(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        loaded = manager._load_session(session.session_id)
        assert loaded is not None
        assert loaded.pairing_code == session.pairing_code

    def test_validate_pairing_code_found(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        found = manager.validate_pairing_code(session.pairing_code)
        assert found is not None
        assert found.session_id == session.session_id

    def test_validate_pairing_code_case_insensitive(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        found = manager.validate_pairing_code(session.pairing_code.lower())
        assert found is not None

    def test_validate_pairing_code_not_found(self, manager: PairingManager) -> None:
        assert manager.validate_pairing_code("DEADBEEF") is None

    def test_validate_pairing_code_expired(self, manager: PairingManager) -> None:
        session = manager.generate_pairing(timeout_seconds=0)
        # Ensure expiry
        time.sleep(0.01)
        assert manager.validate_pairing_code(session.pairing_code) is None

    def test_complete_pairing_success(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        totp_code = generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(
            session.session_id,
            totp_code,
            device_name="TestPhone",
            device_type=DeviceType.PHONE.value,
            capabilities=[DeviceCapability.PUSH_NOTIFICATIONS.value],
        )
        assert device is not None
        assert device.name == "TestPhone"
        assert device.device_type == DeviceType.PHONE.value
        assert DeviceCapability.PUSH_NOTIFICATIONS.value in device.capabilities

        # Session should be marked as paired
        loaded_session = manager._load_session(session.session_id)
        assert loaded_session is not None
        assert loaded_session.status == PairingStatus.PAIRED.value

    def test_complete_pairing_bad_totp(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        device = manager.complete_pairing(
            session.session_id,
            "000000",
            device_name="BadPhone",
        )
        assert device is None
        # Session should be rejected
        loaded = manager._load_session(session.session_id)
        assert loaded is not None
        assert loaded.status == PairingStatus.REJECTED.value

    def test_complete_pairing_expired_session(self, manager: PairingManager) -> None:
        session = manager.generate_pairing(timeout_seconds=0)
        time.sleep(0.01)
        totp_code = generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(
            session.session_id,
            totp_code,
            device_name="LatePhone",
        )
        assert device is None

    def test_complete_pairing_unknown_session(self, manager: PairingManager) -> None:
        device = manager.complete_pairing("deadbeef" * 4, "123456", "Phone")
        assert device is None

    def test_complete_pairing_already_paired(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        totp_code = generate_totp_code(session.totp_secret)
        manager.complete_pairing(session.session_id, totp_code, "Phone1")
        # Second attempt should fail (not pending anymore)
        device = manager.complete_pairing(session.session_id, totp_code, "Phone2")
        assert device is None


class TestDeviceManagement:
    def test_list_devices_empty(self, manager: PairingManager) -> None:
        assert manager.list_devices() == []

    def test_list_devices_after_pairing(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        totp_code = generate_totp_code(session.totp_secret)
        manager.complete_pairing(session.session_id, totp_code, "Phone")
        devices = manager.list_devices()
        assert len(devices) == 1
        assert devices[0].name == "Phone"

    def test_get_device(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        totp_code = generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(session.session_id, totp_code, "Phone")
        assert device is not None
        fetched = manager.get_device(device.id)
        assert fetched is not None
        assert fetched.name == "Phone"

    def test_get_device_not_found(self, manager: PairingManager) -> None:
        assert manager.get_device("ab" * 16) is None

    def test_remove_device(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        totp_code = generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(session.session_id, totp_code, "Phone")
        assert device is not None
        assert manager.remove_device(device.id) is True
        assert manager.get_device(device.id) is None

    def test_remove_device_not_found(self, manager: PairingManager) -> None:
        assert manager.remove_device("ab" * 16) is False

    def test_update_last_seen(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        totp_code = generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(session.session_id, totp_code, "Phone")
        assert device is not None
        original_last_seen = device.last_seen
        time.sleep(0.01)
        assert manager.update_last_seen(device.id) is True
        updated = manager.get_device(device.id)
        assert updated is not None
        assert updated.last_seen >= original_last_seen

    def test_update_last_seen_not_found(self, manager: PairingManager) -> None:
        assert manager.update_last_seen("ab" * 16) is False

    def test_verify_device_token_valid(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        totp_code = generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(session.session_id, totp_code, "Phone")
        assert device is not None
        assert manager.verify_device_token(device.id, device.auth_token) is True

    def test_verify_device_token_invalid(self, manager: PairingManager) -> None:
        session = manager.generate_pairing()
        totp_code = generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(session.session_id, totp_code, "Phone")
        assert device is not None
        assert manager.verify_device_token(device.id, "wrong-token") is False

    def test_verify_device_token_unknown_device(self, manager: PairingManager) -> None:
        assert manager.verify_device_token("ab" * 16, "token") is False


class TestCleanupExpiredSessions:
    def test_cleanup_expired(self, manager: PairingManager) -> None:
        manager.generate_pairing(timeout_seconds=0)
        time.sleep(0.01)
        removed = manager.cleanup_expired_sessions()
        assert removed == 1

    def test_cleanup_skips_active(self, manager: PairingManager) -> None:
        manager.generate_pairing(timeout_seconds=3600)
        removed = manager.cleanup_expired_sessions()
        assert removed == 0


class TestPathValidation:
    def test_invalid_device_id_raises(self, manager: PairingManager) -> None:
        with pytest.raises(ValueError, match="Invalid device id"):
            manager._device_path("../etc/passwd")

    def test_invalid_session_id_raises(self, manager: PairingManager) -> None:
        with pytest.raises(ValueError, match="Invalid session id"):
            manager._session_path("../../bad")

    def test_valid_hex_id_accepted(self, manager: PairingManager) -> None:
        path = manager._device_path("ab" * 16)
        assert path.name == f"{'ab' * 16}.json"


class TestCorruptFiles:
    def test_corrupt_device_file_skipped(self, manager: PairingManager) -> None:
        corrupt = manager._devices_dir / "abcd1234abcd1234abcd1234abcd1234.json"
        corrupt.write_text("not valid json{{{", encoding="utf-8")
        devices = manager.list_devices()
        assert len(devices) == 0

    def test_corrupt_session_file_returns_none(self, manager: PairingManager) -> None:
        session_id = "ab" * 16
        corrupt = manager._sessions_dir / f"{session_id}.json"
        corrupt.write_text("broken", encoding="utf-8")
        assert manager._load_session(session_id) is None
