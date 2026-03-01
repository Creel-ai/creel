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
        patch("creel.orchestrator._exec_gmail_readonly_inline") as mock_gmail,
        patch("creel.orchestrator.run_llm") as mock_llm,
        patch("creel.orchestrator.send_output"),
    ):
        mock_gmail.return_value = json.dumps(
            [
                {"subject": "Important", "from": "boss@example.com", "snippet": "Need reply"},
            ]
        )
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
    """Test that _run_executor_inline dispatches to the correct executor."""

    def _cfg(self, **args) -> ExecutorConfig:
        return ExecutorConfig(args=args)

    @pytest.mark.parametrize(
        "name,mock_target",
        [
            ("weather", "creel.orchestrator._exec_weather_inline"),
            ("calendar", "creel.orchestrator._exec_gcal_inline"),
            ("gcal_write", "creel.orchestrator._exec_gcal_write_inline"),
            ("gmail_send", "creel.orchestrator._exec_gmail_send_inline"),
            ("drive", "creel.orchestrator._exec_drive_inline"),
            ("drive_write", "creel.orchestrator._exec_drive_write_inline"),
            ("apple_notes", "creel.orchestrator._exec_apple_notes_inline"),
            ("apple_reminders", "creel.orchestrator._exec_apple_reminders_inline"),
            ("brave_search", "creel.orchestrator._exec_brave_search_inline"),
            ("notion", "creel.orchestrator._exec_notion_inline"),
            ("notion_write", "creel.orchestrator._exec_notion_write_inline"),
            ("fetch_url", "creel.orchestrator._exec_fetch_url_inline"),
            ("google_docs", "creel.orchestrator._exec_google_docs_inline"),
            ("google_sheets", "creel.orchestrator._exec_google_sheets_inline"),
            ("google_slides", "creel.orchestrator._exec_google_slides_inline"),
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
            "creel.orchestrator._exec_gmail_readonly_inline",
            return_value="emails",
        ) as mock_fn:
            result = _run_executor_inline("gmail_readonly", cfg)
        assert result == "emails"
        mock_fn.assert_called_once_with(cfg)

    def test_dispatch_gmail_modify(self) -> None:
        cfg = self._cfg()
        with patch(
            "creel.orchestrator._exec_gmail_modify_inline",
            return_value="modified",
        ) as mock_fn:
            result = _run_executor_inline("gmail_modify", cfg)
        assert result == "modified"
        mock_fn.assert_called_once_with(cfg)

    def test_dispatch_exec(self) -> None:
        cfg = self._cfg(command="echo hi")
        with patch("creel.orchestrator._exec_exec_inline", return_value="hi") as mock_fn:
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
            "creel.orchestrator._exec_bluebubbles_inline",
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
        from creel.secrets import encrypt_env_file

        env_file = tmp_path / "exec.env"
        env_file.write_text("MY_EXEC_SECRET=s3cret\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        cfg = ExecutorConfig(secrets=str(enc_path), args={"location": "denver"})

        captured_env = {}

        def fake_weather(config):
            captured_env["MY_EXEC_SECRET"] = os.environ.get("MY_EXEC_SECRET")
            return '{"ok": true}'

        with patch(
            "creel.orchestrator._exec_weather_inline",
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
        from creel.secrets import encrypt_env_file

        env_file = tmp_path / "exec.env"
        env_file.write_text("TEMP_SECRET=val\n")
        enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

        cfg = ExecutorConfig(secrets=str(enc_path), args={})

        with (
            patch(
                "creel.orchestrator._exec_weather_inline",
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
            patch(
                "creel.orchestrator._exec_weather_inline",
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


# ---------------------------------------------------------------------------
# Google Docs/Sheets/Slides inline handler tests
# ---------------------------------------------------------------------------


class TestGoogleDocsInline:
    def test_google_docs_inline_dispatches_read(self) -> None:
        cfg = ExecutorConfig(args={"action": "read", "document_id": "doc-1"})
        with patch(
            "executors.google_docs.executor.read_document",
            return_value={"documentId": "doc-1", "title": "T", "content": "c"},
        ) as mock_read:
            result = _run_executor_inline("google_docs", cfg)
        assert "doc-1" in result
        mock_read.assert_called_once_with("doc-1")

    def test_google_docs_inline_dispatches_create(self) -> None:
        cfg = ExecutorConfig(args={"action": "create", "title": "New Doc", "body": "Hello"})
        with patch(
            "executors.google_docs.executor.create_document",
            return_value={"documentId": "new", "url": "http://x"},
        ) as mock_create:
            result = _run_executor_inline("google_docs", cfg)
        assert "new" in result
        mock_create.assert_called_once_with("New Doc", "Hello")

    def test_google_docs_inline_dispatches_append(self) -> None:
        cfg = ExecutorConfig(args={"action": "append", "document_id": "doc-1", "text": "more"})
        with patch(
            "executors.google_docs.executor.append_text",
            return_value={"documentId": "doc-1", "appended": True},
        ) as mock_append:
            result = _run_executor_inline("google_docs", cfg)
        assert "appended" in result
        mock_append.assert_called_once_with("doc-1", "more")

    def test_google_docs_inline_dispatches_replace(self) -> None:
        cfg = ExecutorConfig(
            args={
                "action": "replace",
                "document_id": "doc-1",
                "find": "old",
                "replace_with": "new",
            }
        )
        with patch(
            "executors.google_docs.executor.replace_text",
            return_value={"documentId": "doc-1", "occurrencesChanged": 2},
        ) as mock_replace:
            result = _run_executor_inline("google_docs", cfg)
        assert "2" in result
        mock_replace.assert_called_once_with("doc-1", "old", "new", True)

    def test_google_docs_inline_dispatches_insert(self) -> None:
        cfg = ExecutorConfig(
            args={
                "action": "insert",
                "document_id": "doc-1",
                "text": "inserted",
                "index": "5",
            }
        )
        with patch(
            "executors.google_docs.executor.insert_text",
            return_value={"documentId": "doc-1", "inserted": True},
        ) as mock_insert:
            result = _run_executor_inline("google_docs", cfg)
        assert "inserted" in result
        mock_insert.assert_called_once_with("doc-1", "inserted", 5)

    def test_google_docs_inline_unknown_action_raises(self) -> None:
        cfg = ExecutorConfig(args={"action": "bad"})
        with pytest.raises(ValueError, match="unknown action"):
            _run_executor_inline("google_docs", cfg)


class TestGoogleSheetsInline:
    def test_google_sheets_inline_dispatches_read(self) -> None:
        cfg = ExecutorConfig(
            args={
                "action": "read",
                "spreadsheet_id": "sheet-1",
                "range": "A1:B2",
            }
        )
        with patch(
            "executors.google_sheets.executor.read_sheet",
            return_value={"range": "A1:B2", "values": [["a"]]},
        ) as mock_read:
            result = _run_executor_inline("google_sheets", cfg)
        assert "A1:B2" in result
        mock_read.assert_called_once_with("sheet-1", "A1:B2")

    def test_google_sheets_inline_dispatches_write(self) -> None:
        cfg = ExecutorConfig(
            args={
                "action": "write",
                "spreadsheet_id": "sheet-1",
                "range": "A1",
                "data": '[["x"]]',
            }
        )
        with patch(
            "executors.google_sheets.executor.write_to_sheet",
            return_value={"updatedCells": 1},
        ) as mock_write:
            result = _run_executor_inline("google_sheets", cfg)
        assert "1" in result
        mock_write.assert_called_once_with("sheet-1", "A1", '[["x"]]', "USER_ENTERED")

    def test_google_sheets_inline_dispatches_create(self) -> None:
        cfg = ExecutorConfig(args={"action": "create", "title": "New Sheet"})
        with patch(
            "executors.google_sheets.executor.create_spreadsheet",
            return_value={"spreadsheetId": "new", "url": "http://x"},
        ) as mock_create:
            result = _run_executor_inline("google_sheets", cfg)
        assert "new" in result
        mock_create.assert_called_once_with("New Sheet", "", "")

    def test_google_sheets_inline_dispatches_append(self) -> None:
        cfg = ExecutorConfig(
            args={
                "action": "append",
                "spreadsheet_id": "sheet-1",
                "range": "A:B",
                "data": '[["y"]]',
            }
        )
        with patch(
            "executors.google_sheets.executor.append_to_sheet",
            return_value={"updatedCells": 1},
        ) as mock_append:
            result = _run_executor_inline("google_sheets", cfg)
        assert "1" in result
        mock_append.assert_called_once_with("sheet-1", "A:B", '[["y"]]', "USER_ENTERED")

    def test_google_sheets_inline_unknown_action_raises(self) -> None:
        cfg = ExecutorConfig(args={"action": "bad"})
        with pytest.raises(ValueError, match="unknown action"):
            _run_executor_inline("google_sheets", cfg)


class TestGoogleSlidesInline:
    def test_google_slides_inline_dispatches_read(self) -> None:
        cfg = ExecutorConfig(args={"action": "read", "presentation_id": "pres-1"})
        with patch(
            "executors.google_slides.executor.read_presentation",
            return_value={"presentationId": "pres-1", "slideCount": 2, "slides": []},
        ) as mock_read:
            result = _run_executor_inline("google_slides", cfg)
        assert "pres-1" in result
        mock_read.assert_called_once_with("pres-1")

    def test_google_slides_inline_dispatches_create(self) -> None:
        cfg = ExecutorConfig(args={"action": "create", "title": "New Pres"})
        with patch(
            "executors.google_slides.executor.create_presentation",
            return_value={"presentationId": "new", "url": "http://x"},
        ) as mock_create:
            result = _run_executor_inline("google_slides", cfg)
        assert "new" in result
        mock_create.assert_called_once_with("New Pres")

    def test_google_slides_inline_dispatches_add_slide(self) -> None:
        cfg = ExecutorConfig(
            args={
                "action": "add_slide",
                "presentation_id": "pres-1",
                "title": "Slide Title",
                "body": "Slide body",
            }
        )
        with patch(
            "executors.google_slides.executor.add_slide",
            return_value={"presentationId": "pres-1", "slideId": "s1"},
        ) as mock_add:
            result = _run_executor_inline("google_slides", cfg)
        assert "s1" in result
        mock_add.assert_called_once_with("pres-1", "Slide Title", "Slide body", "BLANK")

    def test_google_slides_inline_dispatches_replace_text(self) -> None:
        cfg = ExecutorConfig(
            args={
                "action": "replace_text",
                "presentation_id": "pres-1",
                "find": "old",
                "replace_with": "new",
            }
        )
        with patch(
            "executors.google_slides.executor.replace_text",
            return_value={"presentationId": "pres-1", "occurrencesChanged": 3},
        ) as mock_replace:
            result = _run_executor_inline("google_slides", cfg)
        assert "3" in result
        mock_replace.assert_called_once_with("pres-1", "old", "new", True)

    def test_google_slides_inline_unknown_action_raises(self) -> None:
        cfg = ExecutorConfig(args={"action": "bad"})
        with pytest.raises(ValueError, match="unknown action"):
            _run_executor_inline("google_slides", cfg)


# ---------------------------------------------------------------------------
# E2E orchestrator pipeline tests for Google Docs/Sheets/Slides
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

        with (
            patch("creel.orchestrator._exec_google_sheets_inline") as mock_sheets,
            patch("creel.orchestrator.run_llm") as mock_llm,
            patch("creel.orchestrator.send_output"),
        ):
            mock_sheets.return_value = json.dumps({"values": [["Name", "Age"], ["Alice", "30"]]})
            mock_llm.return_value = "The sheet contains Alice, age 30."
            result = run_task(path)

        assert result == "The sheet contains Alice, age 30."
        mock_sheets.assert_called_once()
        prompt = mock_llm.call_args[0][0]
        assert "Alice" in prompt

    def test_google_docs_executor_through_orchestrator(self, tmp_path: Path) -> None:
        """Docs executor should work through the full task pipeline."""
        task = {
            "name": "docs_test",
            "schedule": "0 8 * * *",
            "executors": {
                "google_docs": {
                    "args": {"action": "read", "document_id": "doc-xyz"},
                }
            },
            "prompt": "Date: {date}\nDocument: {google_docs}",
            "output": {"type": "stdout", "to": ""},
            "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
        }
        path = tmp_path / "docs_test.yaml"
        path.write_text(yaml.dump(task))

        with (
            patch("creel.orchestrator._exec_google_docs_inline") as mock_docs,
            patch("creel.orchestrator.run_llm") as mock_llm,
            patch("creel.orchestrator.send_output"),
        ):
            mock_docs.return_value = json.dumps(
                {"documentId": "doc-xyz", "title": "Report", "content": "Q1 revenue was $1M."}
            )
            mock_llm.return_value = "The document reports Q1 revenue of $1M."
            result = run_task(path)

        assert result == "The document reports Q1 revenue of $1M."
        mock_docs.assert_called_once()
        prompt = mock_llm.call_args[0][0]
        assert "Q1 revenue" in prompt

    def test_google_slides_executor_through_orchestrator(self, tmp_path: Path) -> None:
        """Slides executor should work through the full task pipeline."""
        task = {
            "name": "slides_test",
            "schedule": "0 8 * * *",
            "executors": {
                "google_slides": {
                    "args": {"action": "read", "presentation_id": "pres-abc"},
                }
            },
            "prompt": "Date: {date}\nPresentation: {google_slides}",
            "output": {"type": "stdout", "to": ""},
            "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
        }
        path = tmp_path / "slides_test.yaml"
        path.write_text(yaml.dump(task))

        with (
            patch("creel.orchestrator._exec_google_slides_inline") as mock_slides,
            patch("creel.orchestrator.run_llm") as mock_llm,
            patch("creel.orchestrator.send_output"),
        ):
            mock_slides.return_value = json.dumps(
                {"presentationId": "pres-abc", "title": "Q1 Review", "slideCount": 3, "slides": []}
            )
            mock_llm.return_value = "The presentation has 3 slides about Q1."
            result = run_task(path)

        assert result == "The presentation has 3 slides about Q1."
        mock_slides.assert_called_once()
        prompt = mock_llm.call_args[0][0]
        assert "Q1 Review" in prompt

    def test_google_docs_executor_failure_continues(self, tmp_path: Path) -> None:
        """If Docs executor fails, task should continue with error placeholder."""
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
                "creel.orchestrator._exec_google_docs_inline",
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
