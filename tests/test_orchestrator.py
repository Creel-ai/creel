"""Tests for the orchestrator."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from creel.models import ExecutorConfig
from creel.orchestrator import (
    _load_secrets_to_env,
    _run_executor_inline,
    run_task,
)


def _make_task(tmp_path: Path, **overrides) -> Path:
    """Helper to create a task YAML file."""
    task = {
        "name": "test_task",
        "schedule": "0 7 * * *",
        "executors": {
            "weather": {
                "args": {"location": "denver"},
            }
        },
        "prompt": "Date: {date}\nWeather: {weather}",
        "output": {"type": "stdout", "to": ""},
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
    }
    task.update(overrides)
    path = tmp_path / f"{task['name']}.yaml"
    path.write_text(yaml.dump(task))
    return path


# ---------------------------------------------------------------------------
# Existing run_task tests
# ---------------------------------------------------------------------------


def test_dry_run(tmp_path: Path) -> None:
    """Dry run should render the prompt without calling LLM or output."""
    task_path = _make_task(tmp_path)

    with patch("creel.orchestrator._run_executor_inline") as mock_fetch:
        mock_fetch.return_value = '{"temp_f": "72", "condition": "sunny"}'
        result = run_task(task_path, dry_run=True)

    assert "Weather:" in result
    assert "72" in result
    assert "sunny" in result
    assert "Date:" in result


def test_run_task_calls_llm_and_output(tmp_path: Path) -> None:
    """Full run should call executor, LLM, and output."""
    task_path = _make_task(tmp_path)

    with (
        patch("creel.orchestrator._run_executor_inline") as mock_fetch,
        patch("creel.orchestrator.run_llm") as mock_llm,
        patch("creel.orchestrator.send_output") as mock_output,
    ):
        mock_fetch.return_value = '{"temp": "70"}'
        mock_llm.return_value = "It's a nice day!"

        result = run_task(task_path)

    assert result == "It's a nice day!"
    mock_llm.assert_called_once()
    mock_output.assert_called_once()

    # Verify the prompt was rendered and passed to LLM
    llm_call_args = mock_llm.call_args
    prompt = llm_call_args[0][0]
    assert "70" in prompt


def test_gmail_executor_through_orchestrator(tmp_path: Path) -> None:
    """Gmail executor branch should work through the orchestrator (via skill registry)."""
    task = {
        "name": "gmail_test",
        "schedule": "0 8 * * *",
        "executors": {
            "gmail_readonly": {
                "args": {"query": "is:unread", "max_results": "5", "full_body": "false"},
            }
        },
        "prompt": "Date: {date}\nEmails: {gmail_readonly}",
        "output": {"type": "stdout", "to": ""},
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
    }
    path = tmp_path / "gmail_test.yaml"
    path.write_text(yaml.dump(task))

    email_json = json.dumps(
        [{"subject": "Important", "from": "boss@example.com", "snippet": "Need reply"}]
    )

    with (
        patch("creel.orchestrator._run_executor_inline", return_value=email_json),
        patch("creel.orchestrator.run_llm") as mock_llm,
        patch("creel.orchestrator.send_output"),
    ):
        mock_llm.return_value = "You have 1 email from your boss."
        result = run_task(path)

    assert result == "You have 1 email from your boss."
    llm_call_args = mock_llm.call_args
    prompt = llm_call_args[0][0]
    assert "Important" in prompt
    assert "boss@example.com" in prompt


def test_executor_failure_continues(tmp_path: Path) -> None:
    """If a executor fails, the task should continue with an error placeholder."""
    task_path = _make_task(tmp_path)

    with (
        patch("creel.orchestrator._run_executor_inline") as mock_fetch,
        patch("creel.orchestrator.run_llm") as mock_llm,
        patch("creel.orchestrator.send_output"),
    ):
        mock_fetch.side_effect = RuntimeError("Connection timeout")
        mock_llm.return_value = "No weather data available."

        result = run_task(task_path)

    assert result == "No weather data available."
    # The prompt should contain the error placeholder
    llm_call_args = mock_llm.call_args
    prompt = llm_call_args[0][0]
    assert "[Error fetching weather" in prompt


# ---------------------------------------------------------------------------
# Inline executor dispatcher tests
# ---------------------------------------------------------------------------


class TestRunExecutorInline:
    """Test that _run_executor_inline dispatches via the skill registry."""

    def _cfg(self, **args) -> ExecutorConfig:
        return ExecutorConfig(args=args)

    def test_dispatch_weather(self) -> None:
        """_run_executor_inline should find the weather skill and call execute."""
        cfg = self._cfg(location="denver")
        with patch(
            "executors.weather.executor.fetch_weather",
            return_value={"temp_f": "72"},
        ):
            result = _run_executor_inline("weather", cfg)
        assert "72" in result

    def test_unknown_executor_raises(self) -> None:
        cfg = self._cfg()
        with pytest.raises(ValueError, match="Unknown inline executor"):
            _run_executor_inline("nonexistent", cfg)


# ---------------------------------------------------------------------------
# Secret / env handling tests
# ---------------------------------------------------------------------------


class TestExecutorSecrets:
    def _patch_weather_skill(self, side_effect):
        """Patch the weather skill's execute function within the registry.

        _run_executor_inline creates its own SkillRegistry, so we mock the
        skill entry that gets discovered.
        """
        from creel.skills.registry import SkillEntry, SkillRegistry

        original_discover = SkillRegistry._discover_builtins

        def patched_discover(self_reg):
            original_discover(self_reg)
            entry = self_reg.get_skill("weather")
            if entry is not None:
                # Replace the execute function
                self_reg._entries["weather"] = SkillEntry(meta=entry.meta, execute=side_effect)

        return patch.object(SkillRegistry, "_discover_builtins", patched_discover)

    def test_secrets_loaded_and_restored(self, tmp_path, age_keypair, monkeypatch) -> None:
        """_run_executor_inline should load secrets and restore env after."""
        key_file, pub_file = age_keypair
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))
        from creel.secrets import encrypt_env_file

        env_file = tmp_path / "exec.env"
        env_file.write_text("MY_EXEC_SECRET=s3cret\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        cfg = ExecutorConfig(secrets=str(enc_path), args={"location": "denver"})

        captured_env = {}

        def fake_weather(config):
            captured_env["MY_EXEC_SECRET"] = os.environ.get("MY_EXEC_SECRET")
            return '{"ok": true}'

        with self._patch_weather_skill(fake_weather):
            _run_executor_inline("weather", cfg)

        assert captured_env["MY_EXEC_SECRET"] == "s3cret"
        # Should be cleaned up after
        assert os.environ.get("MY_EXEC_SECRET") is None

    def test_secrets_restored_on_exception(self, tmp_path, age_keypair, monkeypatch) -> None:
        """Env vars should be restored even if executor raises."""
        key_file, pub_file = age_keypair
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))
        from creel.secrets import encrypt_env_file

        env_file = tmp_path / "exec.env"
        env_file.write_text("TEMP_SECRET=val\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        cfg = ExecutorConfig(secrets=str(enc_path), args={})

        def raising_weather(config):
            raise RuntimeError("boom")

        with self._patch_weather_skill(raising_weather), pytest.raises(RuntimeError):
            _run_executor_inline("weather", cfg)

        assert os.environ.get("TEMP_SECRET") is None

    def test_google_credentials_json_replaced_with_access_token(
        self, tmp_path, age_keypair, monkeypatch
    ) -> None:
        """Inline executor env should receive GOOGLE_ACCESS_TOKEN, not refresh JSON."""
        key_file, pub_file = age_keypair
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))
        from creel.secrets import encrypt_env_file

        creds_json = json.dumps(
            {
                "refresh_token": "rt",
                "client_id": "cid",
                "client_secret": "cs",
            }
        )
        env_file = tmp_path / "google.env"
        env_file.write_text(f"GOOGLE_CREDENTIALS_JSON={creds_json}\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        cfg = ExecutorConfig(secrets=str(enc_path), args={})
        captured_env: dict[str, str | None] = {}

        def fake_weather(config):
            captured_env["GOOGLE_ACCESS_TOKEN"] = os.environ.get("GOOGLE_ACCESS_TOKEN")
            captured_env["GOOGLE_CREDENTIALS_JSON"] = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            return '{"ok": true}'

        with (
            patch(
                "creel.oauth.get_google_access_token_from_json",
                return_value="ya29.inline-token",
            ) as mock_token,
            self._patch_weather_skill(fake_weather),
        ):
            _run_executor_inline("weather", cfg)

        assert captured_env["GOOGLE_ACCESS_TOKEN"] == "ya29.inline-token"
        assert captured_env["GOOGLE_CREDENTIALS_JSON"] is None
        mock_token.assert_called_once()


class TestLoadSecretsToEnv:
    def test_loads_and_sets(self, tmp_path, age_keypair) -> None:
        key_file, pub_file = age_keypair
        from creel.secrets import encrypt_env_file

        env_file = tmp_path / "llm.env"
        env_file.write_text("LLM_SECRET=abc123\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        # Ensure key can be found
        os.environ["AGE_IDENTITY_FILE"] = str(key_file)
        try:
            _load_secrets_to_env(str(enc_path))
            assert os.environ.get("LLM_SECRET") == "abc123"
        finally:
            os.environ.pop("LLM_SECRET", None)
            os.environ.pop("AGE_IDENTITY_FILE", None)


# ---------------------------------------------------------------------------
# Agent mode tests
# ---------------------------------------------------------------------------


class TestAgentMode:
    def test_agent_mode_calls_run_agent_loop(self, tmp_path) -> None:
        task_path = _make_task(
            tmp_path,
            mode="agent",
            tools={
                "weather": {
                    "executor": "weather",
                    "description": "Get weather",
                }
            },
        )
        mock_result = MagicMock()
        mock_result.text = "Agent response"
        mock_result.turns_used = 2
        mock_result.tool_calls_made = 1
        mock_result.stop_reason = "end_turn"

        with (
            patch("creel.orchestrator._run_executor_inline", return_value='{"ok":1}'),
            patch("creel.orchestrator.run_llm"),
            patch("creel.orchestrator.send_output"),
            patch("creel.agent.run_agent_loop", return_value=mock_result) as mock_loop,
        ):
            result = run_task(task_path)

        assert result == "Agent response"
        mock_loop.assert_called_once()

    def test_agent_mode_container(self, tmp_path) -> None:
        task_path = _make_task(
            tmp_path,
            mode="agent",
            tools={
                "weather": {
                    "executor": "weather",
                    "description": "Get weather",
                }
            },
        )
        mock_result = MagicMock()
        mock_result.text = "Container agent"
        mock_result.turns_used = 1
        mock_result.tool_calls_made = 0
        mock_result.stop_reason = "end_turn"

        with (
            patch("creel.orchestrator._run_executor_inline", return_value='{"ok":1}'),
            patch("creel.orchestrator.run_llm"),
            patch("creel.orchestrator.send_output"),
            patch(
                "creel.container_agent.run_agent_loop_container",
                return_value=mock_result,
            ) as mock_loop,
        ):
            result = run_task(task_path, use_containers=True)

        assert result == "Container agent"
        mock_loop.assert_called_once()

    def test_llm_secrets_loaded(self, tmp_path, age_keypair) -> None:
        key_file, pub_file = age_keypair
        from creel.secrets import encrypt_env_file

        env_file = tmp_path / "llm.env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-test\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        task_path = _make_task(
            tmp_path,
            llm={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 100,
                "secrets": str(enc_path),
            },
        )

        os.environ["AGE_IDENTITY_FILE"] = str(key_file)
        try:
            with (
                patch("creel.orchestrator._run_executor_inline", return_value="{}"),
                patch("creel.orchestrator.run_llm", return_value="ok"),
                patch("creel.orchestrator.send_output"),
            ):
                run_task(task_path)
            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-test"
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("AGE_IDENTITY_FILE", None)


# ---------------------------------------------------------------------------
# E2E orchestrator pipeline tests (patching _run_executor_inline)
# ---------------------------------------------------------------------------


class TestGoogleExecutorsE2E:
    def test_google_sheets_executor_through_orchestrator(self, tmp_path: Path) -> None:
        """Sheets executor should work through the full task pipeline."""
        task = {
            "name": "sheets_test",
            "schedule": "0 8 * * *",
            "executors": {
                "google_sheets": {
                    "args": {
                        "action": "read",
                        "spreadsheet_id": "abc123",
                        "range": "Sheet1!A1:B2",
                    },
                }
            },
            "prompt": "Date: {date}\nSheet data: {google_sheets}",
            "output": {"type": "stdout", "to": ""},
            "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
        }
        path = tmp_path / "sheets_test.yaml"
        path.write_text(yaml.dump(task))

        sheets_data = json.dumps({"values": [["Name", "Age"], ["Alice", "30"]]})
        with (
            patch("creel.orchestrator._run_executor_inline", return_value=sheets_data),
            patch("creel.orchestrator.run_llm") as mock_llm,
            patch("creel.orchestrator.send_output"),
        ):
            mock_llm.return_value = "The sheet contains Alice, age 30."
            result = run_task(path)

        assert result == "The sheet contains Alice, age 30."
        prompt = mock_llm.call_args[0][0]
        assert "Alice" in prompt

    def test_google_docs_executor_failure_continues(self, tmp_path: Path) -> None:
        """If executor fails, task should continue with error placeholder."""
        task = {
            "name": "docs_fail_test",
            "schedule": "0 8 * * *",
            "executors": {
                "google_docs": {
                    "args": {"action": "read", "document_id": "doc-bad"},
                }
            },
            "prompt": "Date: {date}\nDocument: {google_docs}",
            "output": {"type": "stdout", "to": ""},
            "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
        }
        path = tmp_path / "docs_fail_test.yaml"
        path.write_text(yaml.dump(task))

        with (
            patch(
                "creel.orchestrator._run_executor_inline",
                side_effect=RuntimeError("API quota exceeded"),
            ),
            patch("creel.orchestrator.run_llm") as mock_llm,
            patch("creel.orchestrator.send_output"),
        ):
            mock_llm.return_value = "Could not read the document."
            result = run_task(path)

        assert result == "Could not read the document."
        prompt = mock_llm.call_args[0][0]
        assert "[Error fetching google_docs" in prompt
