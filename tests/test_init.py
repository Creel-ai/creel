"""Tests for creel init — directory scaffolding, templates, wizard, and migration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from creel import paths
from creel.init import (
    InitChannelConfig,
    InitConfig,
    InitGuardianConfig,
    InitLLMConfig,
    InitTelegramConfig,
    _build_tools_section,
    _encrypt_secrets,
    _ensure_age_keypair,
    _generate_agent_yaml,
    _load_catalog,
    _prompt_multi_select,
    _send_test_message,
    init,
    migrate,
)
from creel.validation import ValidationResult


@pytest.fixture()
def mock_age(monkeypatch, age_keypair):
    """Monkeypatch ``_ensure_age_keypair`` to use the session-scoped test keypair."""
    key_file, pub_file = age_keypair
    monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))
    return key_file, pub_file


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

    def test_already_initialized_decline_reconfigure(self, monkeypatch, tmp_path):
        """When already initialized and user declines reconfigure, should exit early."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        init()  # first init
        assert paths.is_initialized()

        # Simulate user declining reconfigure ("n")
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

        lines = init()  # second init, no --force
        assert any("Already initialized" in line for line in lines)


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

        # Wizard flow: provider(1) → model(default) → tools(none) → channel(Terminal=1)
        #   → guardian(yes) → policy(yes) → audit(yes) → media(no)
        inputs = self._make_inputs(
            [
                "1",  # provider: Anthropic
                "",  # model: default
                "none",  # tools: none
                "1",  # channel: Terminal (CLI only)
                "",  # guardian: yes (default)
                "",  # policy: yes (default)
                "",  # audit: yes (default)
                "n",  # media: no
            ]
        )
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

    def test_wizard_telegram_flow(self, monkeypatch, tmp_path, mock_age):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        inputs = self._make_inputs(
            [
                "2",  # provider: OpenAI
                "",  # model: default
                "none",  # tools: none
                "2",  # channel: Telegram
                "alice,bob",  # allowed senders
                "n",  # test message: no
                "",  # guardian: yes (default)
                "",  # policy: yes (default)
                "",  # audit: yes (default)
                "n",  # media: no
            ]
        )
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
            lambda token: ValidationResult(
                ok=True, message="Bot valid", detail={"username": "mybot"}
            ),
        )
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

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

    def test_wizard_declines_invalid_api_key(self, monkeypatch, tmp_path):
        """After 3 failed validations, declining should clear the key."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        # provider(1) → decline bad key(n) → model(default) → tools(none)
        #   → channel(Terminal=1) → guardian(yes) → policy(yes) → audit(yes) → media(no)
        inputs = self._make_inputs(
            [
                "1",  # provider: Anthropic
                "n",  # decline invalid key
                "",  # model: default
                "none",  # tools: none
                "1",  # channel: Terminal
                "",  # guardian: yes (default)
                "",  # policy: yes (default)
                "",  # audit: yes (default)
                "n",  # media: no
            ]
        )
        monkeypatch.setattr("builtins.input", inputs)

        # getpass returns a key 3 times
        getpass_values = iter(["sk-ant-bad-key"] * 3)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(getpass_values))

        # Mock validator to always fail
        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: ValidationResult(ok=False, message="Invalid API key (401 Unauthorized)"),
        )

        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

        init(force=True)

        agent_yaml = tmp_path / "home" / "agent.yaml"
        config = yaml.safe_load(agent_yaml.read_text())
        # No secrets should be present since the key was declined
        assert "secrets" not in config["llm"]

    def test_wizard_google_provider(self, monkeypatch, tmp_path, mock_age):
        """Google (Gemini) provider should use correct default model."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        inputs = self._make_inputs(
            [
                "3",  # provider: Google
                "",  # model: default (gemini-2.0-flash)
                "none",  # tools: none
                "1",  # channel: Terminal
                "",  # guardian: yes
                "",  # policy: yes
                "",  # audit: yes
                "n",  # media: no
            ]
        )
        monkeypatch.setattr("builtins.input", inputs)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "AIza-test-key")

        monkeypatch.setattr(
            "creel.validation.validate_google_key",
            lambda key: ValidationResult(ok=True, message="API key is valid"),
        )
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

        init(force=True)

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert config["llm"]["model"] == "gemini-2.0-flash"
        assert config["llm"]["secrets"] == "secrets/google.env.enc"

    def test_wizard_with_tools_selection(self, monkeypatch, tmp_path, mock_age):
        """Selecting tools should generate tool definitions in agent.yaml."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        inputs = self._make_inputs(
            [
                "1",  # provider: Anthropic
                "",  # model: default
                "5,6",  # tools: weather, github
                "1",  # channel: Terminal
                "",  # guardian: yes
                "",  # policy: yes
                "",  # audit: yes
                "n",  # media: no
            ]
        )
        monkeypatch.setattr("builtins.input", inputs)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-test-key")

        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: ValidationResult(ok=True, message="OK"),
        )
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

        lines = init(force=True)

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert "check_weather" in config["tools"]
        assert config["tools"]["check_weather"]["executor"] == "weather"
        assert "github" in config["tools"]
        assert any("tools:" in line for line in lines)

    def test_wizard_whatsapp_channel(self, monkeypatch, tmp_path, mock_age):
        """Selecting WhatsApp channel should generate whatsapp config."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        inputs = self._make_inputs(
            [
                "1",  # provider: Anthropic
                "",  # model: default
                "none",  # tools: none
                "4",  # channel: WhatsApp
                "",  # guardian: yes
                "",  # policy: yes
                "",  # audit: yes
                "n",  # media: no
            ]
        )
        monkeypatch.setattr("builtins.input", inputs)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-test-key")

        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: ValidationResult(ok=True, message="OK"),
        )
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

        lines = init(force=True)

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert "whatsapp" in config["channels"]
        assert any("WHATSAPP" in line for line in lines)

    def test_wizard_guardian_disabled(self, monkeypatch, tmp_path, mock_age):
        """Disabling guardian should omit guardian section."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        inputs = self._make_inputs(
            [
                "1",  # provider: Anthropic
                "",  # model: default
                "none",  # tools: none
                "1",  # channel: Terminal
                "n",  # guardian: no
                "n",  # media: no
            ]
        )
        monkeypatch.setattr("builtins.input", inputs)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-test-key")

        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: ValidationResult(ok=True, message="OK"),
        )
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))

        init(force=True)

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert "guardian" not in config


# ---------------------------------------------------------------------------
# Non-interactive init tests
# ---------------------------------------------------------------------------


class TestNonInteractiveInit:
    def test_anthropic_minimal(self, monkeypatch, tmp_path, mock_age):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

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

    def test_with_telegram_channel(self, monkeypatch, tmp_path, mock_age):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

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

    def test_google_provider(self, monkeypatch, tmp_path, mock_age):
        """Google provider should use gemini default model and GOOGLE_API_KEY env var."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        key_file, _pub_file = mock_age

        init(interactive=False, provider="google", api_key="AIza-test")

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert config["llm"]["model"] == "gemini-2.0-flash"
        assert config["llm"]["secrets"] == "secrets/google.env.enc"

        # Verify the encrypted secret uses GOOGLE_API_KEY
        from creel.secrets import decrypt_env_file

        enc_path = tmp_path / "home" / "secrets" / "google.env.enc"
        env = decrypt_env_file(enc_path, identity_path=str(key_file))
        assert env["GOOGLE_API_KEY"] == "AIza-test"

    def test_with_tools(self, monkeypatch, tmp_path):
        """Tools passed via CLI should appear in generated agent.yaml."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        init(
            interactive=False,
            provider="anthropic",
            tools=["weather", "shell"],
        )

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert "check_weather" in config["tools"]
        assert "run_command" in config["tools"]

    def test_whatsapp_channel(self, monkeypatch, tmp_path):
        """WhatsApp channel should generate whatsapp config."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        init(interactive=False, provider="anthropic", channel="whatsapp")

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert "whatsapp" in config["channels"]
        assert config["channels"]["whatsapp"]["api_url"] == "$WHATSAPP_API_URL"

    def test_all_tools(self, monkeypatch, tmp_path):
        """Selecting all tools should generate all tool definitions."""
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))

        init(
            interactive=False,
            provider="anthropic",
            tools=["gmail", "calendar", "drive", "web_search", "weather", "github", "shell"],
        )

        config = yaml.safe_load((tmp_path / "home" / "agent.yaml").read_text())
        assert "check_email" in config["tools"]
        assert "send_email" in config["tools"]
        assert "check_calendar" in config["tools"]
        assert "create_event" in config["tools"]
        assert "search_drive" in config["tools"]
        assert "web_search" in config["tools"]
        assert "check_weather" in config["tools"]
        assert "github" in config["tools"]
        assert "run_command" in config["tools"]


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

    def test_whatsapp_channel(self):
        config = self._make_config(channel=InitChannelConfig(type="whatsapp"))
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert "whatsapp" in doc["channels"]
        assert config.channel.type == "whatsapp"

    def test_no_guardian_when_disabled(self):
        config = self._make_config(enable_guardian=False)
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert "guardian" not in doc

    def test_guardian_with_policy_and_audit(self):
        config = self._make_config(
            guardian=InitGuardianConfig(policy=True, audit=False),
        )
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert doc["guardian"]["policy"]["enabled"] is True
        assert doc["guardian"]["audit"]["enabled"] is False

    def test_media_section_when_enabled(self):
        config = self._make_config(enable_media=True)
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert doc["media"]["enabled"] is True

    def test_with_tools(self):
        config = self._make_config(tools=["weather", "github"])
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        assert "check_weather" in doc["tools"]
        assert doc["tools"]["check_weather"]["executor"] == "weather"
        assert "github" in doc["tools"]
        assert doc["tools"]["github"]["executor"] == "github"

    def test_no_tools_has_example_comment(self):
        config = self._make_config()
        content = _generate_agent_yaml(config, {})
        assert "# Example tool" in content

    def test_with_tools_no_example_comment(self):
        config = self._make_config(tools=["weather"])
        content = _generate_agent_yaml(config, {})
        assert "# Example tool" not in content

    def test_yaml_round_trips_through_agent_definition(self):
        """Generated YAML should parse as a valid AgentDefinition."""
        from creel.models import AgentDefinition

        config = self._make_config()
        content = _generate_agent_yaml(config, {})
        doc = yaml.safe_load(content)
        agent_def = AgentDefinition(**doc)
        assert agent_def.llm.model == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# Tool building tests
# ---------------------------------------------------------------------------


class TestToolCatalog:
    def test_catalog_loads(self):
        catalog = _load_catalog()
        assert len(catalog) >= 8

    def test_catalog_structure(self):
        catalog = _load_catalog()
        for group_id, group in catalog.items():
            assert "label" in group, f"group {group_id!r} missing 'label'"
            assert "tools" in group, f"group {group_id!r} missing 'tools'"
            assert isinstance(group["tools"], dict)
            assert len(group["tools"]) >= 1

    def test_catalog_has_expected_groups(self):
        catalog = _load_catalog()
        expected = {
            "gmail",
            "calendar",
            "drive",
            "web_search",
            "weather",
            "github",
            "notion",
            "shell",
        }
        assert expected <= set(catalog.keys())


class TestBuildToolsSection:
    def test_empty_selection(self):
        tools = _build_tools_section([])
        assert tools == {}

    def test_single_tool(self):
        tools = _build_tools_section(["weather"])
        assert "check_weather" in tools
        assert tools["check_weather"]["executor"] == "weather"

    def test_gmail_has_two_tools(self):
        tools = _build_tools_section(["gmail"])
        assert "check_email" in tools
        assert "send_email" in tools

    def test_calendar_has_two_tools(self):
        tools = _build_tools_section(["calendar"])
        assert "check_calendar" in tools
        assert "create_event" in tools

    def test_unknown_tool_ignored(self):
        tools = _build_tools_section(["nonexistent", "weather"])
        assert "check_weather" in tools
        assert len(tools) == 1

    def test_all_tools(self):
        tools = _build_tools_section(
            ["gmail", "calendar", "drive", "web_search", "weather", "github", "shell"]
        )
        # Should have tools from all categories
        assert len(tools) >= 7


# ---------------------------------------------------------------------------
# Multi-select prompt tests
# ---------------------------------------------------------------------------


class TestPromptMultiSelect:
    def test_all_default(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        result = _prompt_multi_select("Pick", ["A", "B"], ["a", "b"])
        assert result == ["a", "b"]

    def test_none(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "none")
        result = _prompt_multi_select("Pick", ["A", "B"], ["a", "b"])
        assert result == []

    def test_specific_indices(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "1,3")
        result = _prompt_multi_select("Pick", ["A", "B", "C"], ["a", "b", "c"])
        assert result == ["a", "c"]

    def test_all_keyword(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "all")
        result = _prompt_multi_select("Pick", ["A", "B"], ["a", "b"])
        assert result == ["a", "b"]

    def test_invalid_index_ignored(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "1,99,abc")
        result = _prompt_multi_select("Pick", ["A", "B"], ["a", "b"])
        assert result == ["a"]


# ---------------------------------------------------------------------------
# Test message tests
# ---------------------------------------------------------------------------


class TestSendTestMessage:
    def test_telegram_success(self, monkeypatch):
        import httpx as _httpx

        resp = MagicMock(status_code=200)
        monkeypatch.setattr(_httpx, "post", lambda *a, **kw: resp)
        cfg = InitChannelConfig(
            type="telegram",
            telegram=InitTelegramConfig(bot_token="123:TOK", allowed_senders=["alice"]),
        )
        assert _send_test_message("telegram", cfg) is True

    def test_telegram_failure(self, monkeypatch):
        import httpx as _httpx

        resp = MagicMock(status_code=401)
        monkeypatch.setattr(_httpx, "post", lambda *a, **kw: resp)
        cfg = InitChannelConfig(
            type="telegram",
            telegram=InitTelegramConfig(bot_token="bad", allowed_senders=["alice"]),
        )
        assert _send_test_message("telegram", cfg) is False

    def test_no_telegram_config(self):
        cfg = InitChannelConfig(type="none")
        assert _send_test_message("telegram", cfg) is False

    def test_empty_allowed_senders(self):
        cfg = InitChannelConfig(
            type="telegram",
            telegram=InitTelegramConfig(bot_token="123:TOK", allowed_senders=[]),
        )
        assert _send_test_message("telegram", cfg) is False


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

    def test_google_env_var_name(self, monkeypatch, tmp_path, age_keypair):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / "home"))
        key_file, pub_file = age_keypair
        monkeypatch.setattr("creel.init._ensure_age_keypair", lambda: (key_file, pub_file))

        config = InitConfig(
            llm=InitLLMConfig(provider="google", model="gemini-2.0-flash", api_key="AIza-test"),
        )
        _encrypt_secrets(config)

        from creel.secrets import decrypt_env_file

        enc_path = tmp_path / "home" / "secrets" / "google.env.enc"
        env = decrypt_env_file(enc_path, identity_path=str(key_file))
        assert env["GOOGLE_API_KEY"] == "AIza-test"


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
