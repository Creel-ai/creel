"""Shared fixtures for integration test scenarios.

Provides httpx clients connected to the daemon (via UDS) and mock LLM server.
"""

from __future__ import annotations

import os

import httpx
import pytest


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set — run via test-harness.sh or harness.py")
    return value


@pytest.fixture(scope="session")
def daemon_socket() -> str:
    """Path to the daemon Unix domain socket."""
    return _require_env("HARNESS_DAEMON_SOCKET")


@pytest.fixture(scope="session")
def mock_llm_url() -> str:
    """Base URL of the mock LLM server."""
    return _require_env("HARNESS_MOCK_LLM_URL")


@pytest.fixture(scope="session")
def daemon_client(daemon_socket: str) -> httpx.Client:
    """httpx client connected to the daemon via Unix domain socket."""
    transport = httpx.HTTPTransport(uds=daemon_socket)
    with httpx.Client(
        transport=transport,
        base_url="http://daemon",
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture(scope="session")
def mock_client(mock_llm_url: str) -> httpx.Client:
    """httpx client connected to the mock LLM server."""
    with httpx.Client(base_url=mock_llm_url, timeout=10.0) as client:
        yield client


@pytest.fixture(autouse=True)
def reset_mock_llm(mock_client: httpx.Client):
    """Reset mock LLM state before each test for isolation."""
    mock_client.post("/v1/mock/reset")
    yield
