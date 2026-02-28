"""Shared fixtures and helpers for integration test scenarios.

Provides httpx clients connected to the daemon (via UDS) and mock LLM server,
plus common helper functions used across scenario test files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def send_message(
    client: httpx.Client,
    text: str,
    sender_id: str = "test-sender",
    session_id: str | None = None,
    auto_approve: bool = False,
) -> httpx.Response:
    """POST /v1/messages and return the raw response."""
    body: dict = {"sender_id": sender_id, "text": text, "auto_approve": auto_approve}
    if session_id is not None:
        body["session_id"] = session_id
    return client.post("/v1/messages", json=body)


def get_tool_result_from_followup_call(history: dict) -> dict | None:
    """Get the tool_result block from the second (followup) mock LLM call.

    In a tool-call flow with a clean mock reset:
      Call 0: user text -> LLM returns tool_use
      Call 1: messages include tool_result -> LLM returns followup text

    The tool_result in Call 1's LAST user message is the one from the
    current test's tool execution (or Guardian block).
    """
    calls = history.get("calls", [])
    if len(calls) < 2:
        return None

    followup_call = calls[1]
    messages = followup_call.get("body", {}).get("messages", [])

    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return block
    return None


def read_audit_entries(audit_log_path: Path) -> list[dict]:
    """Read all entries from the JSONL audit log."""
    if not audit_log_path.exists():
        return []
    entries = []
    with open(audit_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture(scope="session")
def config_dir() -> Path:
    """Path to the test harness config directory (daemon CWD)."""
    return Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(scope="session")
def audit_log_path(config_dir: Path) -> Path:
    """Path to the Guardian audit log (JSONL)."""
    return config_dir / "guardian_audit.jsonl"


@pytest.fixture(autouse=True)
def reset_mock_llm(mock_client: httpx.Client):
    """Reset mock LLM state before each test for isolation."""
    mock_client.post("/v1/mock/reset")
    yield
