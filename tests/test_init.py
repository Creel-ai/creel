"""Tests for creel init — directory scaffolding, templates, wizard, and migration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pyrage
import yaml

from creel import paths
from creel.init import (
    InitChannelConfig,
    InitConfig,
    InitLLMConfig,
    InitTelegramConfig,
    _encrypt_secrets,
    _ensure_age_keypair,
    _generate_agent_yaml,
    init,
    migrate,
)
from creel.validation import ValidationResult

# ---------------------------------------------------------------------------
# Existing tests (static scaffold / backward compat)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Interactive wizard tests
# ---------------------------------------------------------------------------


class TestWizard:
    """Test the interactive wizard by monkeypatching input/getpass and validators."""

    def _make_inputs(self, responses: list[str]):
        """Return a side_effect callable that feeds *responses* to input()."""
        it = iter(responses)
        return lambda prompt="": next(it)

    def test_wizard_anthropic_flow(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        # Simulate: Anthropic(1) → api key → model default → no channel(3) → no media → yes guardian
        inputs = self._make_inputs([
            "1",           # provider: Anthropic
            "",            # model: default
            "3",           # channel: none
            "n",           # media: no
            "",            # guardian: yes (default)
        ])
        monkeypatch.setattr("builtins.input", inputs)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-test-key")

        # Mock validator to always succeed
        mock_result = ValidationResult(ok=True, message="API key is valid")
        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: mock_result,
        )

        # Force TTY detection
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

        lines = init(force=True)

        agent_yaml = tmp_path / "home" / "agent.yaml"
        assert agent_yaml.exists()
        config = yaml.safe_load(agent_yaml.read_text())
        assert config["llm"]["model"] == "claude-sonnet-4-20250514"
        assert "channels" not in config
        assert any("wrote" in line and "agent.yaml" in line for line in lines)

    def test_wizard_telegram_flow(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        inputs = self._make_inputs([
            "2",              # provider: OpenAI
            "",               # model: default
            "1",              # channel: telegram
            "alice,bob",      # allowed senders
            "n",              # media: no
            "y",              # guardian: yes
        ])
        monkeypatch.setattr("builtins.input", inputs)

        # getpass is called twice: API key then bot token
        getpass_values = iter(["sk-openai-key", "123:BOTTOKEN"])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(getpass_values))

        monkeypatch.setattr(
            "creel.validation.validate_openai_key",
            lambda key: ValidationResult(ok=True, message="OK"),
        )
        monkeypatch.setattr(
            "creel.validation.validate_telegram_token",
            lambda token: ValidationResult(ok=True, message="Bot valid", detail={"username": "mybot"}),
        )
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

        # Mock age keypair to avoid touching real ~/.age
        mock_identity = pyrage.x25519.Identity.generate()
        mock_recipient = mock_identity.to_public()
        key_file = tmp_path / "key.txt"
        key_file.write_text(f"# test\n{mock_identity!s}\n")
        pub_file = tmp_path / "key.pub"
        pub_file.write_text(f"{mock_recipient!s}\n")
        monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))

        init(force=True)

        agent_yaml = tmp_path / "home" / "agent.yaml"
        config = yaml.safe_load(agent_yaml.read_text())
        assert config["llm"]["model"] == "gpt-4o"
        assert "telegram" in config["channels"]
        assert config["channels"]["telegram"]["allowed_senders"] == ["alice", "bob"]

        # Verify secrets were encrypted
        secrets_dir = tmp_path / "home" / "secrets"
        assert (secrets_dir / "openai.env.enc").exists()
        assert (secrets_dir / "telegram.env.enc").exists()


# ---------------------------------------------------------------------------
# Non-interactive init tests
# ---------------------------------------------------------------------------


class TestNonInteractiveInit:
    def test_anthropic_minimal(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        # Mock age keypair
        mock_identity = pyrage.x25519.Identity.generate()
        mock_recipient = mock_identity.to_public()
        key_file = tmp_path / "key.txt"
        key_file.write_text(f"# test\n{mock_identity!s}\n")
        pub_file = tmp_path / "key.pub"
        pub_file.write_text(f"{mock_recipient!s}\n")
        monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))

        lines = init(
            interactive=False,
            provider="anthropic",
            api_key="sk-ant-test",
            model="claude-sonnet-4-20250514",
        )

        agent_yaml = tmp_path / "home" / "agent.yaml"
        assert agent_yaml.exists()
        config = yaml.safe_load(agent_yaml.read_text())
        assert config["llm"]["model"] == "claude-sonnet-4-20250514"
        assert config["llm"]["secrets"] == "secrets/anthropic.env.enc"
        assert any("agent.yaml" in line for line in lines)

    def test_ollama_no_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        init(
            interactive=False,
            provider="ollama",
            model="llama3",
        )

        agent_yaml = tmp_path / "home" / "agent.yaml"
        config = yaml.safe_load(agent_yaml.read_text())
        assert config["llm"]["model"] == "llama3"
        assert "secrets" not in config["llm"]

    def test_with_telegram_channel(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        mock_identity = pyrage.x25519.Identity.generate()
        mock_recipient = mock_identity.to_public()
        key_file = tmp_path / "key.txt"
        key_file.write_text(f"# test\n{mock_identity!s}\n")
        pub_file = tmp_path / "key.pub"
        pub_file.write_text(f"{mock_recipient!s}\n")
        monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))

        init(
            interactive=False,
            provider="anthropic",
            api_key="sk-ant-test",
            channel="telegram",
            bot_token="123:ABC",
            allowed_senders="alice,bob",
        )

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert config["channels"]["telegram"]["allowed_senders"] == ["alice", "bob"]
        assert config["channels"]["telegram"]["secrets"] == "secrets/telegram.env.enc"

    def test_default_model_per_provider(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        init(interactive=False, provider="openai")

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert config["llm"]["model"] == "gpt-4o"

    def test_guardian_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        init(interactive=False, provider="anthropic", enable_guardian=False)

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert "guardian" not in config

    def test_media_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        init(interactive=False, provider="anthropic", enable_media=True)

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert config["media"]["enabled"] is True


# ---------------------------------------------------------------------------
# YAML generation tests
# ---------------------------------------------------------------------------


class TestGenerateAgentYaml:
    def _make_config(self, **overrides) -> InitConfig:
        defaults = dict(
            llm=InitLLMConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
            channel=InitChannelConfig(type="none"),
            enable_media=False,
            enable_guardian=True,
        )
        defaults.update(overrides)
        return InitConfig(**defaults)

    def test_minimal_anthropic(self):
        config = self._make_config()
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert doc["llm"]["model"] == "claude-sonnet-4-20250514"
        assert "system_prompt" in doc
        assert "channels" not in doc

    def test_with_llm_secrets(self):
        config = self._make_config()
        content = _generate_agent_yaml(config, {"llm": "secrets/anthropic.env.enc"})
        doc = yaml.safe_load(content)
        assert doc["llm"]["secrets"] == "secrets/anthropic.env.enc"

    def test_telegram_channel(self):
        config = self._make_config(
            channel=InitChannelConfig(
                type="telegram",
                telegram=InitTelegramConfig(bot_token="tok", allowed_senders=["alice"]),
            ),
        )
        content = _generate_agent_yaml(config, {"telegram": "secrets/telegram.env.enc"})
        doc = yaml.safe_load(content)
        assert doc["channels"]["telegram"]["allowed_senders"] == ["alice"]
        assert doc["channels"]["telegram"]["secrets"] == "secrets/telegram.env.enc"

    def test_imessage_channel(self):
        config = self._make_config(channel=InitChannelConfig(type="imessage"))
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert "imessage" in doc["channels"]

    def test_no_guardian_when_disabled(self):
        config = self._make_config(enable_guardian=False)
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert "guardian" not in doc

    def test_media_section_when_enabled(self):
        config = self._make_config(enable_media=True)
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert doc["media"]["enabled"] is True

    def test_yaml_round_trips_through_agent_definition(self):
        """Generated YAML should parse as a valid AgentDefinition."""
        from creel.models import AgentDefinition

        config = self._make_config()
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        agent_def = AgentDefinition(**doc)
        assert agent_def.llm.model == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# Age keypair tests
# ---------------------------------------------------------------------------


class TestEnsureAgeKeypair:
    def test_creates_keypair_if_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        key_file, pub_file = _ensure_age_keypair()

        assert key_file.exists()
        assert pub_file.exists()
        assert "AGE-SECRET-KEY-" in key_file.read_text()
        assert pub_file.read_text().strip().startswith("age1")

    def test_reuses_existing_keypair(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Create first
        key_file1, pub_file1 = _ensure_age_keypair()
        original_key = key_file1.read_text()

        # Call again — should not regenerate
        key_file2, pub_file2 = _ensure_age_keypair()
        assert key_file2.read_text() == original_key

    def test_key_file_permissions(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        key_file, _ = _ensure_age_keypair()

        # key.txt should be owner-read/write only
        mode = key_file.stat().st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Secret encryption tests
# ---------------------------------------------------------------------------


class TestEncryptSecrets:
    def test_encrypts_api_key(self, monkeypatch, tmp_path, age_keypair):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        key_file, pub_file = age_keypair

        monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))

        config = InitConfig(
            llm=InitLLMConfig(provider="anthropic", model="test", api_key="sk-ant-secret"),
        )
        result = _encrypt_secrets(config)

        assert "llm" in result
        assert result["llm"] == "secrets/anthropic.env.enc"

        # Verify decryptable
        from creel.secrets import decrypt_env_file

        enc_path = tmp_path / "home" / "secrets" / "anthropic.env.enc"
        assert enc_path.exists()
        env = decrypt_env_file(enc_path, identity_path=str(key_file))
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-secret"

    def test_encrypts_telegram_token(self, monkeypatch, tmp_path, age_keypair):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        key_file, pub_file = age_keypair
        monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))

        config = InitConfig(
            llm=InitLLMConfig(provider="anthropic", model="test"),
            channel=InitChannelConfig(
                type="telegram",
                telegram=InitTelegramConfig(bot_token="123:TOKEN", allowed_senders=["alice"]),
            ),
        )
        result = _encrypt_secrets(config)

        assert "telegram" in result
        from creel.secrets import decrypt_env_file

        enc_path = tmp_path / "home" / "secrets" / "telegram.env.enc"
        env = decrypt_env_file(enc_path, identity_path=str(key_file))
        assert env["TELEGRAM_BOT_TOKEN"] == "123:TOKEN"

    def test_no_secrets_when_no_keys(self, monkeypatch, tmp_path, age_keypair):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        key_file, pub_file = age_keypair
        monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))

        config = InitConfig(
            llm=InitLLMConfig(provider="ollama", model="llama3"),
        )
        result = _encrypt_secrets(config)
        assert result == {}

    def test_openai_env_var_name(self, monkeypatch, tmp_path, age_keypair):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        key_file, pub_file = age_keypair
        monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))

        config = InitConfig(
            llm=InitLLMConfig(provider="openai", model="gpt-4o", api_key="sk-openai-test"),
        )
        _encrypt_secrets(config)

        from creel.secrets import decrypt_env_file

        enc_path = tmp_path / "home" / "secrets" / "openai.env.enc"
        env = decrypt_env_file(enc_path, identity_path=str(key_file))
        assert "OPENAI_API_KEY" in env


# ---------------------------------------------------------------------------
# Migration tests (unchanged)
# ---------------------------------------------------------------------------


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
