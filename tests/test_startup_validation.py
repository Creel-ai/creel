"""Tests for startup secrets validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.startup import SecretsValidationError, validate_secrets


def _make_agent_def(llm_secrets=None, tools=None):
    agent = MagicMock()
    agent.llm.secrets = llm_secrets
    agent.tools = tools or {}
    return agent


class TestValidateSecrets:
    def test_no_secrets_passes(self):
        """No secrets referenced should pass silently."""
        agent = _make_agent_def()
        validate_secrets(agent)  # should not raise

    def test_missing_enc_file_raises(self, tmp_path: Path):
        agent = _make_agent_def(llm_secrets=str(tmp_path / "nonexistent.enc"))
        with pytest.raises(SecretsValidationError, match="file not found"):
            validate_secrets(agent)

    def test_missing_identity_file_raises(self, tmp_path: Path, monkeypatch):
        # Create a dummy .enc file
        enc = tmp_path / "secrets.enc"
        enc.write_text("dummy")
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(tmp_path / "no-key.txt"))
        agent = _make_agent_def(llm_secrets=str(enc))
        with pytest.raises(SecretsValidationError, match="identity file not found"):
            validate_secrets(agent)

    def test_tool_secrets_validated(self, tmp_path: Path):
        tool = MagicMock()
        tool.secrets = str(tmp_path / "tool_secrets.enc")
        agent = _make_agent_def(tools={"my_tool": tool})
        with pytest.raises(SecretsValidationError, match="tools.my_tool.secrets"):
            validate_secrets(agent)

    @patch("taskrunner.startup.decrypt_env_file")
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

    @patch("taskrunner.startup.decrypt_env_file")
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
