"""Tests for creel init — directory scaffolding, templates, and migration."""

from pathlib import Path

from creel import paths
from creel.init import init, migrate


class TestInit:
    def test_creates_directory_structure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        init()

        home = tmp_path / "home"
        assert home.is_dir()
        assert (home / "policies").is_dir()
        assert (home / "secrets").is_dir()
        assert (home / "sessions").is_dir()
        assert (home / "workspace").is_dir()
        assert (home / "cron").is_dir()

    def test_copies_agent_yaml_template(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        init()

        agent = tmp_path / "home" / "agent.yaml"
        assert agent.exists()
        content = agent.read_text()
        assert "system_prompt" in content

    def test_copies_policy_template(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        init()

        policy = tmp_path / "home" / "policies" / "default.yaml"
        assert policy.exists()
        content = policy.read_text()
        assert "allow:" in content

    def test_does_not_overwrite_without_force(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        agent = tmp_path / "home" / "agent.yaml"
        agent.parent.mkdir(parents=True)
        agent.write_text("custom config\n")

        init(force=False)

        assert agent.read_text() == "custom config\n"

    def test_overwrites_with_force(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        agent = tmp_path / "home" / "agent.yaml"
        agent.parent.mkdir(parents=True)
        agent.write_text("custom config\n")

        init(force=True)

        content = agent.read_text()
        assert "system_prompt" in content
        assert content != "custom config\n"

    def test_is_initialized_after_init(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        assert not paths.is_initialized()

        init()

        assert paths.is_initialized()

    def test_returns_status_lines(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        lines = init()
        assert len(lines) > 0
        assert any("agent.yaml" in line for line in lines)


class TestMigrate:
    def test_copies_agent_yaml_from_repo(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "agent.yaml").write_text("migrated: true\n")

        migrate(repo, force=True)

        migrated = tmp_path / "home" / "agent.yaml"
        assert migrated.exists()
        assert "migrated: true" in migrated.read_text()

    def test_copies_policies(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        (repo / "policies").mkdir(parents=True)
        (repo / "policies" / "custom.yaml").write_text("allow: []\n")

        migrate(repo, force=True)

        assert (tmp_path / "home" / "policies" / "custom.yaml").exists()

    def test_copies_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        (repo / "secrets").mkdir(parents=True)
        (repo / "secrets" / "api.env.enc").write_bytes(b"encrypted-data")

        migrate(repo, force=True)

        assert (tmp_path / "home" / "secrets" / "api.env.enc").exists()

    def test_skips_existing_without_force(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "agent.yaml").write_text("from-repo\n")

        # First init creates template
        init()
        original = (tmp_path / "home" / "agent.yaml").read_text()

        # Migrate without force should not overwrite
        migrate(repo, force=False)
        assert (tmp_path / "home" / "agent.yaml").read_text() == original

    def test_no_config_produces_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        repo = tmp_path / "empty_repo"
        repo.mkdir()

        lines = migrate(repo)
        assert any("nothing to migrate" in line for line in lines)
