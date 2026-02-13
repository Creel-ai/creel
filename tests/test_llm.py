"""Tests for LLM authentication (API key vs auth token)."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from taskrunner.llm import _OAUTH_HEADERS, _run_llm_container, _run_llm_direct
from taskrunner.models import LLMConfig


def _make_config() -> LLMConfig:
    return LLMConfig(model="claude-sonnet-4-20250514", max_tokens=100)


def _mock_message() -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = "Hello"
    msg = MagicMock()
    msg.content = [block]
    return msg


# -- _run_llm_direct auth tests --


@patch("taskrunner.llm.anthropic.Anthropic")
def test_direct_uses_auth_token(mock_cls, monkeypatch):
    """ANTHROPIC_AUTH_TOKEN should be passed as auth_token=."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    mock_cls.return_value.messages.create.return_value = _mock_message()

    result = _run_llm_direct("hi", _make_config())

    mock_cls.assert_called_once_with(
        auth_token="sk-ant-oat01-test", default_headers=_OAUTH_HEADERS,
    )
    assert result == "Hello"


@patch("taskrunner.llm.anthropic.Anthropic")
def test_direct_uses_api_key(mock_cls, monkeypatch):
    """ANTHROPIC_API_KEY should be passed as api_key=."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    mock_cls.return_value.messages.create.return_value = _mock_message()

    result = _run_llm_direct("hi", _make_config())

    mock_cls.assert_called_once_with(api_key="sk-ant-test-key")
    assert result == "Hello"


@patch("taskrunner.llm.anthropic.Anthropic")
def test_direct_auth_token_takes_precedence(mock_cls, monkeypatch):
    """When both are set, auth_token wins."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-key")

    mock_cls.return_value.messages.create.return_value = _mock_message()

    _run_llm_direct("hi", _make_config())

    mock_cls.assert_called_once_with(
        auth_token="sk-ant-oat01-token", default_headers=_OAUTH_HEADERS,
    )


@patch("taskrunner.llm.anthropic.Anthropic")
def test_direct_non_oauth_auth_token_no_headers(mock_cls, monkeypatch):
    """A non-OAuth auth_token should not get the spoofed headers."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-other-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    mock_cls.return_value.messages.create.return_value = _mock_message()

    _run_llm_direct("hi", _make_config())

    mock_cls.assert_called_once_with(
        auth_token="sk-ant-other-token", default_headers={},
    )


def test_direct_no_credentials_raises(monkeypatch):
    """Missing both credentials should raise RuntimeError."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="No Anthropic credentials found"):
        _run_llm_direct("hi", _make_config())


# -- _run_llm_container auth tests --


@patch("taskrunner.orchestrator._ensure_image")
@patch("taskrunner.llm.subprocess.run")
def test_container_passes_auth_token(mock_run, _mock_ensure, monkeypatch):
    """ANTHROPIC_AUTH_TOKEN should be passed to the container."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    mock_run.return_value = MagicMock(stdout="response")

    _run_llm_container("hi", _make_config())

    cmd = mock_run.call_args[0][0]
    assert "-e" in cmd
    idx = cmd.index("ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-token")
    assert cmd[idx - 1] == "-e"


@patch("taskrunner.orchestrator._ensure_image")
@patch("taskrunner.llm.subprocess.run")
def test_container_passes_api_key(mock_run, _mock_ensure, monkeypatch):
    """ANTHROPIC_API_KEY should be passed to the container."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")

    mock_run.return_value = MagicMock(stdout="response")

    _run_llm_container("hi", _make_config())

    cmd = mock_run.call_args[0][0]
    assert "ANTHROPIC_API_KEY=sk-ant-key" in cmd


@patch("taskrunner.orchestrator._ensure_image")
@patch("taskrunner.llm.subprocess.run")
def test_container_passes_both_when_set(mock_run, _mock_ensure, monkeypatch):
    """Both env vars should be forwarded to the container when set."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")

    mock_run.return_value = MagicMock(stdout="response")

    _run_llm_container("hi", _make_config())

    cmd = mock_run.call_args[0][0]
    assert "ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-token" in cmd
    assert "ANTHROPIC_API_KEY=sk-ant-key" in cmd
