"""Tests for the orchestrator."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from taskrunner.models import ExecutorConfig
from taskrunner.orchestrator import (
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

    with patch(
        "taskrunner.orchestrator._run_executor_inline"
    ) as mock_fetch:
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
        patch("taskrunner.orchestrator._run_executor_inline") as mock_fetch,
        patch("taskrunner.orchestrator.run_llm") as mock_llm,
        patch("taskrunner.orchestrator.send_output") as mock_output,
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
    """Gmail executor branch should work through the orchestrator."""
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

    with (
        patch("taskrunner.orchestrator._exec_gmail_readonly_inline") as mock_gmail,
        patch("taskrunner.orchestrator.run_llm") as mock_llm,
        patch("taskrunner.orchestrator.send_output"),
    ):
        mock_gmail.return_value = json.dumps([
            {"subject": "Important", "from": "boss@example.com", "snippet": "Need reply"},
        ])
        mock_llm.return_value = "You have 1 email from your boss."

        result = run_task(path)

    assert result == "You have 1 email from your boss."
    mock_gmail.assert_called_once()

    # Verify the prompt contained the email data
    llm_call_args = mock_llm.call_args
    prompt = llm_call_args[0][0]
    assert "Important" in prompt
    assert "boss@example.com" in prompt


def test_executor_failure_continues(tmp_path: Path) -> None:
    """If a executor fails, the task should continue with an error placeholder."""
    task_path = _make_task(tmp_path)

    with (
        patch("taskrunner.orchestrator._run_executor_inline") as mock_fetch,
        patch("taskrunner.orchestrator.run_llm") as mock_llm,
        patch("taskrunner.orchestrator.send_output"),
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
    """Test that _run_executor_inline dispatches to the correct executor."""

    def _cfg(self, **args) -> ExecutorConfig:
        return ExecutorConfig(args=args)

    @pytest.mark.parametrize(
        "name,mock_target",
        [
            ("weather", "taskrunner.orchestrator._exec_weather_inline"),
            ("calendar", "taskrunner.orchestrator._exec_gcal_inline"),
            ("gcal_write", "taskrunner.orchestrator._exec_gcal_write_inline"),
            ("gmail_send", "taskrunner.orchestrator._exec_gmail_send_inline"),
            ("drive", "taskrunner.orchestrator._exec_drive_inline"),
            ("drive_write", "taskrunner.orchestrator._exec_drive_write_inline"),
            ("apple_notes", "taskrunner.orchestrator._exec_apple_notes_inline"),
            ("apple_reminders", "taskrunner.orchestrator._exec_apple_reminders_inline"),
            ("brave_search", "taskrunner.orchestrator._exec_brave_search_inline"),
            ("fetch_url", "taskrunner.orchestrator._exec_fetch_url_inline"),
        ],
    )
    def test_dispatch_executor(self, name, mock_target) -> None:
        cfg = self._cfg()
        with patch(mock_target, return_value="ok") as mock_fn:
            result = _run_executor_inline(name, cfg)
        assert result == "ok"
        mock_fn.assert_called_once_with(cfg)

    def test_dispatch_gmail_readonly(self) -> None:
        cfg = self._cfg()
        with patch(
            "taskrunner.orchestrator._exec_gmail_readonly_inline",
            return_value="emails",
        ) as mock_fn:
            result = _run_executor_inline("gmail_readonly", cfg)
        assert result == "emails"
        mock_fn.assert_called_once_with(cfg)

    def test_dispatch_gmail_modify(self) -> None:
        cfg = self._cfg()
        with patch(
            "taskrunner.orchestrator._exec_gmail_modify_inline",
            return_value="modified",
        ) as mock_fn:
            result = _run_executor_inline("gmail_modify", cfg)
        assert result == "modified"
        mock_fn.assert_called_once_with(cfg)

    def test_dispatch_exec(self) -> None:
        cfg = self._cfg(command="echo hi")
        with patch(
            "taskrunner.orchestrator._exec_exec_inline", return_value="hi"
        ) as mock_fn:
            result = _run_executor_inline("exec", cfg)
        assert result == "hi"
        mock_fn.assert_called_once_with(cfg)

    @pytest.mark.parametrize(
        "name,action",
        [
            ("bluebubbles", "get_recent_messages"),
            ("bluebubbles_send", "send_message"),
            ("bluebubbles_react", "send_reaction"),
            ("bluebubbles_chats", "get_chats"),
        ],
    )
    def test_dispatch_bluebubbles_variants(self, name, action) -> None:
        cfg = self._cfg()
        with patch(
            "taskrunner.orchestrator._exec_bluebubbles_inline",
            return_value="bb",
        ) as mock_fn:
            result = _run_executor_inline(name, cfg)
        assert result == "bb"
        mock_fn.assert_called_once_with(cfg, action)

    def test_unknown_executor_raises(self) -> None:
        cfg = self._cfg()
        with pytest.raises(ValueError, match="Unknown inline executor"):
            _run_executor_inline("nonexistent", cfg)


# ---------------------------------------------------------------------------
# Secret / env handling tests
# ---------------------------------------------------------------------------


class TestExecutorSecrets:
    def test_secrets_loaded_and_restored(self, tmp_path, age_keypair, monkeypatch) -> None:
        """_run_executor_inline should load secrets and restore env after."""
        key_file, pub_file = age_keypair
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))
        from taskrunner.secrets import encrypt_env_file

        env_file = tmp_path / "exec.env"
        env_file.write_text("MY_EXEC_SECRET=s3cret\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        cfg = ExecutorConfig(secrets=str(enc_path), args={"location": "denver"})

        captured_env = {}

        def fake_weather(config):
            captured_env["MY_EXEC_SECRET"] = os.environ.get("MY_EXEC_SECRET")
            return '{"ok": true}'

        with patch(
            "taskrunner.orchestrator._exec_weather_inline",
            side_effect=fake_weather,
        ):
            _run_executor_inline(
                "weather",
                cfg,
            )

        assert captured_env["MY_EXEC_SECRET"] == "s3cret"
        # Should be cleaned up after
        assert os.environ.get("MY_EXEC_SECRET") is None

    def test_secrets_restored_on_exception(self, tmp_path, age_keypair, monkeypatch) -> None:
        """Env vars should be restored even if executor raises."""
        key_file, pub_file = age_keypair
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))
        from taskrunner.secrets import encrypt_env_file

        env_file = tmp_path / "exec.env"
        env_file.write_text("TEMP_SECRET=val\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        cfg = ExecutorConfig(secrets=str(enc_path), args={})

        with (
            patch(
                "taskrunner.orchestrator._exec_weather_inline",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError),
        ):
            _run_executor_inline("weather", cfg)

        assert os.environ.get("TEMP_SECRET") is None

    def test_google_credentials_json_replaced_with_access_token(
        self, tmp_path, age_keypair, monkeypatch
    ) -> None:
        """Inline executor env should receive GOOGLE_ACCESS_TOKEN, not refresh JSON."""
        key_file, pub_file = age_keypair
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))
        from taskrunner.secrets import encrypt_env_file

        creds_json = json.dumps(
            {
                "refresh_token": "rt",
                "client_id": "cid",
                "client_secret": "cs",
            }
        )
        env_file = tmp_path / "google.env"
        env_file.write_text(f"GOOGLE_CREDENTIALS_JSON={json.dumps(creds_json)}\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        cfg = ExecutorConfig(secrets=str(enc_path), args={})
        captured_env: dict[str, str | None] = {}

        def fake_weather(config):
            captured_env["GOOGLE_ACCESS_TOKEN"] = os.environ.get("GOOGLE_ACCESS_TOKEN")
            captured_env["GOOGLE_CREDENTIALS_JSON"] = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            return '{"ok": true}'

        with (
            patch(
                "taskrunner.oauth.get_google_access_token_from_json",
                return_value="ya29.inline-token",
            ) as mock_token,
            patch(
                "taskrunner.orchestrator._exec_weather_inline",
                side_effect=fake_weather,
            ),
        ):
            _run_executor_inline("weather", cfg)

        assert captured_env["GOOGLE_ACCESS_TOKEN"] == "ya29.inline-token"
        assert captured_env["GOOGLE_CREDENTIALS_JSON"] is None
        mock_token.assert_called_once()


class TestLoadSecretsToEnv:
    def test_loads_and_sets(self, tmp_path, age_keypair) -> None:
        key_file, pub_file = age_keypair
        from taskrunner.secrets import encrypt_env_file

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
            patch("taskrunner.orchestrator._run_executor_inline", return_value='{"ok":1}'),
            patch("taskrunner.orchestrator.run_llm"),
            patch("taskrunner.orchestrator.send_output"),
            patch("taskrunner.agent.run_agent_loop", return_value=mock_result) as mock_loop,
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
            patch("taskrunner.orchestrator._run_executor_inline", return_value='{"ok":1}'),
            patch("taskrunner.orchestrator.run_llm"),
            patch("taskrunner.orchestrator.send_output"),
            patch(
                "taskrunner.container_agent.run_agent_loop_container",
                return_value=mock_result,
            ) as mock_loop,
        ):
            result = run_task(task_path, use_containers=True)

        assert result == "Container agent"
        mock_loop.assert_called_once()

    def test_llm_secrets_loaded(self, tmp_path, age_keypair) -> None:
        key_file, pub_file = age_keypair
        from taskrunner.secrets import encrypt_env_file

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
                patch("taskrunner.orchestrator._run_executor_inline", return_value='{}'),
                patch("taskrunner.orchestrator.run_llm", return_value="ok"),
                patch("taskrunner.orchestrator.send_output"),
            ):
                run_task(task_path)
            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-test"
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("AGE_IDENTITY_FILE", None)


# ---------------------------------------------------------------------------
# Gmail executor specifics
# ---------------------------------------------------------------------------


class TestGmailExecutorInline:
    def test_gmail_readonly_with_message_id(self) -> None:
        cfg = ExecutorConfig(args={"message_id": "msg-123"})
        with patch(
            "executors.gmail_readonly.executor.read_email",
            return_value={"subject": "test"},
        ) as mock_read:
            result = _run_executor_inline("gmail_readonly", cfg)
        assert "test" in result
        mock_read.assert_called_once_with("msg-123")

    def test_gmail_modify_trash(self) -> None:
        cfg = ExecutorConfig(args={"action": "trash", "message_id": "msg-1"})
        with patch(
            "executors.gmail_modify.executor.trash_message",
            return_value={"status": "trashed"},
        ) as mock_trash:
            result = _run_executor_inline("gmail_modify", cfg)
        assert "trashed" in result
        mock_trash.assert_called_once_with("msg-1")

    def test_gmail_modify_delete(self) -> None:
        cfg = ExecutorConfig(args={"action": "delete", "message_id": "msg-2"})
        with patch(
            "executors.gmail_modify.executor.delete_message",
            return_value={"status": "deleted"},
        ) as mock_del:
            result = _run_executor_inline("gmail_modify", cfg)
        assert "deleted" in result
        mock_del.assert_called_once_with("msg-2")

    def test_gmail_modify_unknown_action(self) -> None:
        cfg = ExecutorConfig(args={"action": "unknown", "message_id": "msg-3"})
        with pytest.raises(ValueError, match="unknown action"):
            _run_executor_inline("gmail_modify", cfg)
