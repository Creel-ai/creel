"""Device pairing system for cross-device capabilities.

Allows Creel instances to pair with mobile devices or other machines
for push notifications, camera access, location, clipboard sync, and
remote command execution (with approval).

Pairing flow:
1. Host generates pairing code via ``creel pair generate``
2. Device enters code (web UI or app)
3. Mutual authentication via TOTP challenge-response
4. Paired device info stored locally as JSON
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Pairing code length (hex characters, e.g. "A1B2C3D4")
_PAIRING_CODE_LENGTH = 4  # 4 bytes → 8 hex chars
# Default pairing timeout
_DEFAULT_TIMEOUT_SECONDS = 300
_MIN_TIMEOUT_SECONDS = 1
_MAX_TIMEOUT_SECONDS = 86400  # 24 hours
# TOTP window: ±1 step tolerance for clock skew
_TOTP_STEP_SECONDS = 30
_TOTP_VALID_WINDOW = 1
_TOTP_DIGITS = 6
# Max TOTP attempts before session is rejected
_MAX_TOTP_ATTEMPTS = 3
# Max concurrent pending pairing sessions
_MAX_PENDING_SESSIONS = 10


class DeviceType(StrEnum):
    """Known device types."""

    PHONE = "phone"
    TABLET = "tablet"
    DESKTOP = "desktop"
    LAPTOP = "laptop"
    OTHER = "other"


class DeviceCapability(StrEnum):
    """Capabilities a paired device can advertise."""

    PUSH_NOTIFICATIONS = "push_notifications"
    CAMERA = "camera"
    LOCATION = "location"
    CLIPBOARD = "clipboard"
    REMOTE_EXEC = "remote_exec"


class PairingStatus(StrEnum):
    """State of a pairing session."""

    PENDING = "pending"
    VERIFYING = "verifying"
    PAIRED = "paired"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass
class PairedDevice:
    """A device that has been paired with this Creel instance."""

    id: str = field(default_factory=lambda: secrets.token_hex(16))
    name: str = ""
    device_type: str = DeviceType.OTHER.value
    capabilities: list[str] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)
    paired_at: float = field(default_factory=time.time)
    auth_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PairedDevice:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class PairingSession:
    """An in-progress pairing negotiation."""

    session_id: str = field(default_factory=lambda: secrets.token_hex(16))
    pairing_code: str = ""
    totp_secret: str = ""
    device_name: str = ""
    device_type: str = DeviceType.OTHER.value
    capabilities: list[str] = field(default_factory=list)
    status: str = PairingStatus.PENDING.value
    totp_attempts: int = 0
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_totp_code(secret: str, step: int | None = None) -> str:
    """Generate a TOTP code using HMAC-SHA1.

    Implements RFC 6238 TOTP without external dependencies.
    """
    if step is None:
        step = int(time.time()) // _TOTP_STEP_SECONDS
    msg = step.to_bytes(8, byteorder="big")
    h = hmac.new(secret.encode("utf-8"), msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code_int = (
        ((h[offset] & 0x7F) << 24) | (h[offset + 1] << 16) | (h[offset + 2] << 8) | h[offset + 3]
    )
    code = code_int % (10**_TOTP_DIGITS)
    return str(code).zfill(_TOTP_DIGITS)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code with ±1 window tolerance."""
    current_step = int(time.time()) // _TOTP_STEP_SECONDS
    for offset in range(-_TOTP_VALID_WINDOW, _TOTP_VALID_WINDOW + 1):
        expected = generate_totp_code(secret, current_step + offset)
        if hmac.compare_digest(expected, code):
            return True
    return False


# Filename validation: hex-only device/session IDs
_SAFE_ID_RE = re.compile(r"^[a-f0-9]+$")


class PairingManager:
    """Manages device pairing sessions and paired device storage.

    Data layout on disk::

        <pairing_dir>/
            devices/
                <device_id>.json
            sessions/
                <session_id>.json
    """

    def __init__(self, pairing_dir: str | Path) -> None:
        self._dir = Path(pairing_dir)
        self._devices_dir = self._dir / "devices"
        self._sessions_dir = self._dir / "sessions"
        self._devices_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        # Restrict directory permissions — device files contain auth tokens
        for d in (self._dir, self._devices_dir, self._sessions_dir):
            try:
                d.chmod(0o700)
            except OSError:
                pass
        self._lock = threading.Lock()

    # --- Pairing session lifecycle ---

    def generate_pairing(
        self,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> PairingSession:
        """Generate a new pairing session with a code and TOTP secret.

        Returns a ``PairingSession`` with a human-readable ``pairing_code``
        that the remote device must present, plus a ``totp_secret`` used for
        challenge-response verification.
        """
        timeout_seconds = max(_MIN_TIMEOUT_SECONDS, min(timeout_seconds, _MAX_TIMEOUT_SECONDS))
        # Cap concurrent pending sessions to prevent resource exhaustion
        with self._lock:
            pending = [
                s
                for s in self._list_sessions()
                if s.status == PairingStatus.PENDING.value and not s.is_expired
            ]
            if len(pending) >= _MAX_PENDING_SESSIONS:
                raise RuntimeError(
                    f"Too many pending pairing sessions (max {_MAX_PENDING_SESSIONS})"
                )
        session = PairingSession(
            pairing_code=secrets.token_hex(_PAIRING_CODE_LENGTH).upper(),
            totp_secret=secrets.token_urlsafe(32),
            created_at=time.time(),
            expires_at=time.time() + timeout_seconds,
        )
        self._save_session(session)
        logger.info(
            "Generated pairing session %s (code=%s****, expires in %ds)",
            session.session_id,
            session.pairing_code[:4],
            timeout_seconds,
        )
        return session

    def validate_pairing_code(self, code: str) -> PairingSession | None:
        """Look up a pending session by its pairing code.

        Returns the session if found and still valid, else ``None``.
        """
        code = code.strip().upper()
        with self._lock:
            for session in self._list_sessions():
                if (
                    hmac.compare_digest(session.pairing_code, code)
                    and session.status == PairingStatus.PENDING.value
                    and not session.is_expired
                ):
                    return session
        return None

    def complete_pairing(
        self,
        session_id: str,
        totp_code: str,
        device_name: str,
        device_type: str = DeviceType.OTHER.value,
        capabilities: list[str] | None = None,
    ) -> PairedDevice | None:
        """Complete pairing after the device submits its TOTP response.

        Returns a ``PairedDevice`` on success, ``None`` on failure (bad code,
        expired session, etc.).
        """
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                logger.warning("Pairing session %s not found", session_id)
                return None
            if session.status != PairingStatus.PENDING.value:
                logger.warning("Session %s not in pending state (%s)", session_id, session.status)
                return None
            if session.is_expired:
                session.status = PairingStatus.EXPIRED.value
                self._save_session(session)
                logger.warning("Session %s expired", session_id)
                return None

            # Verify TOTP challenge — allow limited retries
            if not verify_totp(session.totp_secret, totp_code):
                session.totp_attempts += 1
                if session.totp_attempts >= _MAX_TOTP_ATTEMPTS:
                    session.status = PairingStatus.REJECTED.value
                    self._save_session(session)
                    logger.warning(
                        "TOTP verification failed for session %s (max attempts reached)",
                        session_id,
                    )
                else:
                    self._save_session(session)
                    logger.warning(
                        "TOTP verification failed for session %s (attempt %d/%d)",
                        session_id,
                        session.totp_attempts,
                        _MAX_TOTP_ATTEMPTS,
                    )
                return None

            # Success — create the device record
            device = PairedDevice(
                name=device_name,
                device_type=device_type,
                capabilities=capabilities or [],
            )
            self._save_device(device)

            session.status = PairingStatus.PAIRED.value
            session.device_name = device_name
            session.device_type = device_type
            session.capabilities = capabilities or []
            self._save_session(session)

            logger.info("Device %s (%s) paired successfully", device.name, device.id)
            return device

    # --- Device management ---

    def list_devices(self) -> list[PairedDevice]:
        """Return all paired devices."""
        devices: list[PairedDevice] = []
        for path in sorted(self._devices_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                devices.append(PairedDevice.from_dict(data))
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("Skipping corrupt device file: %s", path)
        return devices

    def get_device(self, device_id: str) -> PairedDevice | None:
        """Get a paired device by ID."""
        path = self._device_path(device_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PairedDevice.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def remove_device(self, device_id: str) -> bool:
        """Remove a paired device.  Returns ``True`` if it existed."""
        with self._lock:
            path = self._device_path(device_id)
            if path.is_file():
                path.unlink()
                logger.info("Removed device %s", device_id)
                return True
            return False

    def update_last_seen(self, device_id: str) -> bool:
        """Touch the last_seen timestamp for a device."""
        with self._lock:
            device = self.get_device(device_id)
            if device is None:
                return False
            device.last_seen = time.time()
            self._save_device(device)
            return True

    def verify_device_token(self, device_id: str, token: str) -> bool:
        """Check whether *token* matches the stored auth_token for *device_id*."""
        device = self.get_device(device_id)
        if device is None:
            return False
        return hmac.compare_digest(device.auth_token, token)

    def load_session(self, session_id: str) -> PairingSession | None:
        """Load a pairing session by ID.  Returns ``None`` if not found."""
        return self._load_session(session_id)

    # --- Internal helpers ---

    def _device_path(self, device_id: str) -> Path:
        if not _SAFE_ID_RE.fullmatch(device_id):
            raise ValueError(f"Invalid device id: {device_id!r}")
        return self._devices_dir / f"{device_id}.json"

    def _session_path(self, session_id: str) -> Path:
        if not _SAFE_ID_RE.fullmatch(session_id):
            raise ValueError(f"Invalid session id: {session_id!r}")
        return self._sessions_dir / f"{session_id}.json"

    def _save_device(self, device: PairedDevice) -> None:
        path = self._device_path(device.id)
        path.write_text(json.dumps(device.to_dict(), indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _save_session(self, session: PairingSession) -> None:
        path = self._session_path(session.session_id)
        path.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _load_session(self, session_id: str) -> PairingSession | None:
        path = self._session_path(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PairingSession(
                **{k: v for k, v in data.items() if k in PairingSession.__dataclass_fields__}
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def _list_sessions(self) -> list[PairingSession]:
        sessions: list[PairingSession] = []
        for path in sorted(self._sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                sessions.append(
                    PairingSession(
                        **{
                            k: v
                            for k, v in data.items()
                            if k in PairingSession.__dataclass_fields__
                        }
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("Skipping corrupt session file: %s", path)
        return sessions

    def cleanup_expired_sessions(self) -> int:
        """Mark expired pending sessions and delete their files.  Returns count removed."""
        removed = 0
        with self._lock:
            for session in self._list_sessions():
                if session.is_expired and session.status == PairingStatus.PENDING.value:
                    path = self._session_path(session.session_id)
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed
