"""Tests for the device pairing CLI commands."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from creel.pairing import PairingManager, _generate_totp_code


@pytest.fixture
def pairing_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pairing"
    d.mkdir()
    return d


@pytest.fixture
def manager(pairing_dir: Path) -> PairingManager:
    return PairingManager(pairing_dir)


def _make_args(**kwargs):
    """Create a minimal argparse.Namespace-like object."""
    import argparse

    return argparse.Namespace(**kwargs)


class TestCmdPairGenerate:
    def test_generate_prints_code(self, pairing_dir: Path) -> None:
        from creel.cli import cmd_pair_generate

        with patch("creel.cli._default_pairing_dir", return_value=pairing_dir):
            captured = StringIO()
            with patch("sys.stdout", captured):
                result = cmd_pair_generate(_make_args(timeout=60))

        assert result == 0
        output = captured.getvalue()
        assert "Code:" in output
        assert "Session:" in output
        assert "TOTP verification code:" in output

    def test_generate_custom_timeout(self, pairing_dir: Path) -> None:
        from creel.cli import cmd_pair_generate

        with patch("creel.cli._default_pairing_dir", return_value=pairing_dir):
            captured = StringIO()
            with patch("sys.stdout", captured):
                result = cmd_pair_generate(_make_args(timeout=120))

        assert result == 0
        assert "120 seconds" in captured.getvalue()


class TestCmdPairList:
    def test_list_empty(self, pairing_dir: Path) -> None:
        from creel.cli import cmd_pair_list

        with patch("creel.cli._default_pairing_dir", return_value=pairing_dir):
            captured = StringIO()
            with patch("sys.stdout", captured):
                result = cmd_pair_list(_make_args())

        assert result == 0
        assert "No paired devices" in captured.getvalue()

    def test_list_with_devices(self, pairing_dir: Path, manager: PairingManager) -> None:
        from creel.cli import cmd_pair_list

        # Create a paired device
        session = manager.generate_pairing()
        totp_code = _generate_totp_code(session.totp_secret)
        manager.complete_pairing(
            session.session_id, totp_code, "TestPhone", capabilities=["camera"]
        )

        with patch("creel.cli._default_pairing_dir", return_value=pairing_dir):
            captured = StringIO()
            with patch("sys.stdout", captured):
                result = cmd_pair_list(_make_args())

        assert result == 0
        output = captured.getvalue()
        assert "TestPhone" in output
        assert "camera" in output


class TestCmdPairRemove:
    def test_remove_existing(self, pairing_dir: Path, manager: PairingManager) -> None:
        from creel.cli import cmd_pair_remove

        session = manager.generate_pairing()
        totp_code = _generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(session.session_id, totp_code, "Phone")
        assert device is not None

        with patch("creel.cli._default_pairing_dir", return_value=pairing_dir):
            captured = StringIO()
            with patch("sys.stdout", captured):
                result = cmd_pair_remove(_make_args(device_id=device.id))

        assert result == 0
        assert "removed" in captured.getvalue()

    def test_remove_not_found(self, pairing_dir: Path) -> None:
        from creel.cli import cmd_pair_remove

        with patch("creel.cli._default_pairing_dir", return_value=pairing_dir):
            captured = StringIO()
            with patch("sys.stderr", captured):
                result = cmd_pair_remove(_make_args(device_id="ab" * 16))

        assert result == 1
        assert "not found" in captured.getvalue()


class TestCmdPairTest:
    def test_test_existing_device(self, pairing_dir: Path, manager: PairingManager) -> None:
        from creel.cli import cmd_pair_test

        session = manager.generate_pairing()
        totp_code = _generate_totp_code(session.totp_secret)
        device = manager.complete_pairing(session.session_id, totp_code, "Phone")
        assert device is not None

        with patch("creel.cli._default_pairing_dir", return_value=pairing_dir):
            captured = StringIO()
            with patch("sys.stdout", captured):
                result = cmd_pair_test(_make_args(device_id=device.id))

        assert result == 0
        output = captured.getvalue()
        assert "Phone" in output
        assert "reachable" in output

    def test_test_not_found(self, pairing_dir: Path) -> None:
        from creel.cli import cmd_pair_test

        with patch("creel.cli._default_pairing_dir", return_value=pairing_dir):
            captured = StringIO()
            with patch("sys.stderr", captured):
                result = cmd_pair_test(_make_args(device_id="ab" * 16))

        assert result == 1
        assert "not found" in captured.getvalue()
