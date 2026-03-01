"""Tests for creel.paths — central path resolution."""

from pathlib import Path

from creel import paths


class TestCreelHome:
    def test_default_is_home_dot_creel(self, monkeypatch):
        monkeypatch.delenv("CREEL_HOME", raising=False)
        assert paths.creel_home() == Path.home() / ".creel"

    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "custom"))
        assert paths.creel_home() == tmp_path / "custom"


class TestDerivedPaths:
    def test_agent_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        assert paths.agent_config() == tmp_path / "agent.yaml"

    def test_policies_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        assert paths.policies_dir() == tmp_path / "policies"

    def test_secrets_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        assert paths.secrets_dir() == tmp_path / "secrets"

    def test_sessions_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        assert paths.sessions_dir() == tmp_path / "sessions"

    def test_workspace_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        assert paths.workspace_dir() == tmp_path / "workspace"

    def test_cron_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        assert paths.cron_dir() == tmp_path / "cron"

    def test_audit_log(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        assert paths.audit_log() == tmp_path / "guardian_audit.jsonl"


class TestIsInitialized:
    def test_false_when_no_agent_yaml(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        assert paths.is_initialized() is False

    def test_true_when_agent_yaml_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path))
        (tmp_path / "agent.yaml").write_text("system_prompt: test\n")
        assert paths.is_initialized() is True


class TestCreelExecutable:
    def test_returns_string_or_none(self):
        result = paths.creel_executable()
        assert result is None or isinstance(result, str)
