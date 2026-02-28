"""Tests for OAuth credential hygiene module."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.oauth import (
    _token_cache,
    _token_refresh_log,
    check_credential_freshness,
    clear_token_cache,
    get_google_credentials,
)


@pytest.fixture(autouse=True)
def clean_token_cache():
    """Clear the token cache before and after each test."""
    _token_refresh_log.clear()
    _token_cache.clear()
    yield
    _token_refresh_log.clear()
    _token_cache.clear()


class TestGetGoogleCredentials:
    """Tests for the get_google_credentials function."""

    def test_missing_env_var_raises(self) -> None:
        """Should raise RuntimeError when env var is not set."""
        # Mock the google imports to avoid cryptography issues
        mock_creds_cls = MagicMock()
        mock_request_cls = MagicMock()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.dict(
                "sys.modules",
                {
                    "google.auth.transport.requests": MagicMock(
                        Request=mock_request_cls
                    ),
                    "google.oauth2.credentials": MagicMock(Credentials=mock_creds_cls),
                },
            ),
        ):
            with pytest.raises(RuntimeError, match="not set"):
                get_google_credentials(env_var="MISSING_VAR")

    def test_invalid_json_raises(self) -> None:
        """Should raise RuntimeError for invalid JSON."""
        mock_creds_cls = MagicMock()
        mock_request_cls = MagicMock()

        with (
            patch.dict(os.environ, {"TEST_CREDS": "not json"}),
            patch.dict(
                "sys.modules",
                {
                    "google.auth.transport.requests": MagicMock(
                        Request=mock_request_cls
                    ),
                    "google.oauth2.credentials": MagicMock(Credentials=mock_creds_cls),
                },
            ),
        ):
            with pytest.raises(RuntimeError, match="Invalid JSON"):
                get_google_credentials(env_var="TEST_CREDS")

    def test_missing_fields_raises(self) -> None:
        """Should raise RuntimeError when required fields are missing."""
        creds = json.dumps({"refresh_token": "rt"})
        mock_creds_cls = MagicMock()
        mock_request_cls = MagicMock()

        with (
            patch.dict(os.environ, {"TEST_CREDS": creds}),
            patch.dict(
                "sys.modules",
                {
                    "google.auth.transport.requests": MagicMock(
                        Request=mock_request_cls
                    ),
                    "google.oauth2.credentials": MagicMock(Credentials=mock_creds_cls),
                },
            ),
        ):
            with pytest.raises(RuntimeError, match="Missing required fields"):
                get_google_credentials(env_var="TEST_CREDS")

    def test_refresh_called(self) -> None:
        """Should refresh credentials on each call by default."""
        mock_creds_instance = MagicMock()
        mock_creds_cls = MagicMock(return_value=mock_creds_instance)
        mock_request_cls = MagicMock()

        mock_gauth_mod = MagicMock()
        mock_gauth_mod.Request = mock_request_cls
        mock_gcreds_mod = MagicMock()
        mock_gcreds_mod.Credentials = mock_creds_cls

        creds_data = {
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
        }
        with (
            patch.dict(os.environ, {"TEST_CREDS": json.dumps(creds_data)}),
            patch.dict(
                "sys.modules",
                {
                    "google.auth.transport.requests": mock_gauth_mod,
                    "google.oauth2.credentials": mock_gcreds_mod,
                },
            ),
        ):
            result = get_google_credentials(env_var="TEST_CREDS")

        mock_creds_instance.refresh.assert_called_once()
        assert result is mock_creds_instance

    def test_refresh_timestamp_tracked(self) -> None:
        """Should track the refresh timestamp for audit."""
        mock_creds_cls = MagicMock(return_value=MagicMock())
        mock_request_cls = MagicMock()

        mock_gauth_mod = MagicMock()
        mock_gauth_mod.Request = mock_request_cls
        mock_gcreds_mod = MagicMock()
        mock_gcreds_mod.Credentials = mock_creds_cls

        creds_data = {
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
        }
        with (
            patch.dict(os.environ, {"TEST_CREDS": json.dumps(creds_data)}),
            patch.dict(
                "sys.modules",
                {
                    "google.auth.transport.requests": mock_gauth_mod,
                    "google.oauth2.credentials": mock_gcreds_mod,
                },
            ),
        ):
            get_google_credentials(env_var="TEST_CREDS")

        assert "TEST_CREDS" in _token_refresh_log
        assert _token_refresh_log["TEST_CREDS"] > 0

    def test_refresh_failure_raises(self) -> None:
        """Should raise RuntimeError when token refresh fails."""
        mock_creds = MagicMock()
        mock_creds.refresh.side_effect = Exception("Network error")
        mock_creds_cls = MagicMock(return_value=mock_creds)
        mock_request_cls = MagicMock()

        mock_gauth_mod = MagicMock()
        mock_gauth_mod.Request = mock_request_cls
        mock_gcreds_mod = MagicMock()
        mock_gcreds_mod.Credentials = mock_creds_cls

        creds_data = {
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
        }
        with (
            patch.dict(os.environ, {"TEST_CREDS": json.dumps(creds_data)}),
            patch.dict(
                "sys.modules",
                {
                    "google.auth.transport.requests": mock_gauth_mod,
                    "google.oauth2.credentials": mock_gcreds_mod,
                },
            ),
        ):
            with pytest.raises(RuntimeError, match="Token refresh failed"):
                get_google_credentials(env_var="TEST_CREDS")


class TestCheckCredentialFreshness:
    """Tests for credential freshness checking."""

    def test_never_refreshed(self) -> None:
        """Should report not fresh when never refreshed."""
        result = check_credential_freshness("UNKNOWN_VAR")
        assert result["fresh"] is False
        assert result["last_refresh"] == 0

    def test_recently_refreshed(self) -> None:
        """Should report fresh when recently refreshed."""
        _token_refresh_log["TEST"] = time.time()
        result = check_credential_freshness("TEST")
        assert result["fresh"] is True
        assert result["age_seconds"] < 1.0

    def test_expired_token(self) -> None:
        """Should report not fresh when token is old."""
        _token_refresh_log["TEST"] = time.time() - 7200  # 2 hours ago
        result = check_credential_freshness("TEST", max_age_seconds=3600)
        assert result["fresh"] is False


class TestClearTokenCache:
    """Tests for clearing the token cache."""

    def test_clear_removes_all_entries(self) -> None:
        _token_refresh_log["A"] = time.time()
        _token_refresh_log["B"] = time.time()
        _token_cache["A"] = "tok-a"
        _token_cache["B"] = "tok-b"
        clear_token_cache()
        assert len(_token_refresh_log) == 0
        assert len(_token_cache) == 0
