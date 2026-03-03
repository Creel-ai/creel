"""Tests for creel.validation — API key and bot token validators."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from creel.validation import (
    ValidationResult,
    validate_anthropic_key,
    validate_ollama_reachable,
    validate_openai_key,
    validate_telegram_token,
)


class TestValidateAnthropicKey:
    def test_valid_key(self, monkeypatch):
        resp = MagicMock(status_code=200)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_anthropic_key("sk-ant-valid")
        assert result.ok is True

    def test_invalid_key(self, monkeypatch):
        resp = MagicMock(status_code=401)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_anthropic_key("sk-ant-bad")
        assert result.ok is False
        assert "401" in result.message

    def test_rate_limited_treated_as_valid(self, monkeypatch):
        resp = MagicMock(status_code=429)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_anthropic_key("sk-ant-ratelimited")
        assert result.ok is True
        assert "429" in result.message

    def test_server_error_treated_as_valid(self, monkeypatch):
        resp = MagicMock(status_code=529)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_anthropic_key("sk-ant-overloaded")
        assert result.ok is True

    def test_network_error(self, monkeypatch):
        def raise_err(*a, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "get", raise_err)
        result = validate_anthropic_key("sk-ant-test")
        assert result.ok is False
        assert "Network error" in result.message

    def test_unexpected_status(self, monkeypatch):
        resp = MagicMock(status_code=403)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_anthropic_key("sk-ant-test")
        assert result.ok is False
        assert "403" in result.message


class TestValidateOpenAIKey:
    def test_valid_key(self, monkeypatch):
        resp = MagicMock(status_code=200)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_openai_key("sk-valid")
        assert result.ok is True

    def test_invalid_key(self, monkeypatch):
        resp = MagicMock(status_code=401)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_openai_key("sk-bad")
        assert result.ok is False
        assert "401" in result.message

    def test_rate_limited(self, monkeypatch):
        resp = MagicMock(status_code=429)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_openai_key("sk-ratelimited")
        assert result.ok is True

    def test_network_error(self, monkeypatch):
        def raise_err(*a, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "get", raise_err)
        result = validate_openai_key("sk-test")
        assert result.ok is False


class TestValidateOllamaReachable:
    def test_reachable_with_models(self, monkeypatch):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"models": [{"name": "llama3"}, {"name": "mistral"}]}
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_ollama_reachable("http://localhost:11434")
        assert result.ok is True
        assert result.detail["models"] == ["llama3", "mistral"]
        assert "2 model" in result.message

    def test_unreachable(self, monkeypatch):
        def raise_err(*a, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "get", raise_err)
        result = validate_ollama_reachable("http://localhost:11434")
        assert result.ok is False
        assert "Cannot reach" in result.message

    def test_bad_status(self, monkeypatch):
        resp = MagicMock(status_code=500)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_ollama_reachable("http://localhost:11434")
        assert result.ok is False


class TestValidateTelegramToken:
    def test_valid_token(self, monkeypatch):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ok": True, "result": {"username": "mybot"}}
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_telegram_token("123:ABC")
        assert result.ok is True
        assert result.detail["username"] == "mybot"
        assert "@mybot" in result.message

    def test_invalid_token(self, monkeypatch):
        resp = MagicMock(status_code=401)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: resp)
        result = validate_telegram_token("bad-token")
        assert result.ok is False
        assert "401" in result.message

    def test_network_error(self, monkeypatch):
        def raise_err(*a, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "get", raise_err)
        result = validate_telegram_token("123:ABC")
        assert result.ok is False


class TestValidationResult:
    def test_fields(self):
        r = ValidationResult(ok=True, message="good", detail={"key": "val"})
        assert r.ok is True
        assert r.message == "good"
        assert r.detail == {"key": "val"}
