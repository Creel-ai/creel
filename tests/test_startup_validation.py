"""Tests for startup secrets validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from creel.startup import SecretsValidationError, validate_secrets


def _make_agent_def(llm_secrets=None, tools=None):
    agent = MagicMock()
    agent.llm.secrets = llm_secrets
    agent.skills = tools or {}
    return agent


class TestValidateSecrets:
    def test_no_secrets_passes(self):
        """No secrets referenced should pass silently."""
        agent = _make_agent_def()
        validate_secrets(agent)  # should not raise

    def test_missing_llm_secrets_raises(self, tmp_path: Path):
        """Missing LLM secrets file should raise — the LLM is required."""
        agent = _make_agent_def(llm_secrets=str(tmp_path / "nonexistent.enc"))
        with pytest.raises(SecretsValidationError, match="secrets file not found"):
            validate_secrets(agent)

    def test_missing_identity_file_raises(self, tmp_path: Path, monkeypatch):
        # Create a dummy .enc file
        enc = tmp_path / "secrets.enc"
        enc.write_text("dummy")
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(tmp_path / "no-key.txt"))
        agent = _make_agent_def(llm_secrets=str(enc))
        with pytest.raises(SecretsValidationError, match="identity file not found"):
            validate_secrets(agent)

    def test_tool_secrets_missing_warns(self, tmp_path: Path, caplog):
        """Missing tool secrets should warn, not raise."""
        import logging

        tool = MagicMock()
        tool.secrets = str(tmp_path / "tool_secrets.enc")
        agent = _make_agent_def(tools={"my_tool": tool})
        with caplog.at_level(logging.WARNING):
            validate_secrets(agent)  # should not raise
        assert "skills.my_tool.secrets" in caplog.text
        assert "secrets file not found" in caplog.text

    def test_missing_llm_secrets_relative_path_raises(self, tmp_path: Path, monkeypatch):
        """Missing LLM secrets via relative path should also raise."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        agent = _make_agent_def(llm_secrets="secrets/nonexistent.env.enc")
        with pytest.raises(SecretsValidationError, match="secrets file not found"):
            validate_secrets(agent)

    def test_tool_secrets_missing_relative_path_warns(self, tmp_path: Path, monkeypatch, caplog):
        """Missing tool secrets via relative path should warn, not raise."""
        import logging

        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        tool = MagicMock()
        tool.secrets = "secrets/tool.env.enc"
        agent = _make_agent_def(tools={"my_tool": tool})
        with caplog.at_level(logging.WARNING):
            validate_secrets(agent)  # should not raise
        assert "secrets file not found" in caplog.text

    @patch("creel.startup.decrypt_env_file")
    def test_valid_secrets_passes(self, mock_decrypt, tmp_path: Path, monkeypatch):
        enc = tmp_path / "secrets.enc"
        enc.write_text("encrypted data")
        key = tmp_path / "key.txt"
        key.write_text("AGE-SECRET-KEY-fake")
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key))
        mock_decrypt.return_value = {"API_KEY": "test"}

        agent = _make_agent_def(llm_secrets=str(enc))
        validate_secrets(agent)  # should not raise
        mock_decrypt.assert_called_once_with(str(enc))

    @patch("creel.startup.decrypt_env_file")
    def test_decrypt_failure_raises(self, mock_decrypt, tmp_path: Path, monkeypatch):
        enc = tmp_path / "secrets.enc"
        enc.write_text("bad data")
        key = tmp_path / "key.txt"
        key.write_text("AGE-SECRET-KEY-fake")
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key))
        mock_decrypt.side_effect = RuntimeError("decryption failed")

        agent = _make_agent_def(llm_secrets=str(enc))
        with pytest.raises(SecretsValidationError, match="failed to decrypt"):
            validate_secrets(agent)
