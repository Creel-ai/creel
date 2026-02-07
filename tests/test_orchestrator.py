"""Tests for the orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from taskrunner.orchestrator import run_task


def _make_task(tmp_path: Path, **overrides) -> Path:
    """Helper to create a task YAML file."""
    task = {
        "name": "test_task",
        "schedule": "0 7 * * *",
        "fetch": {
            "weather": {
                "image": "fetcher-weather:latest",
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


def test_dry_run(tmp_path: Path) -> None:
    """Dry run should render the prompt without calling LLM or output."""
    task_path = _make_task(tmp_path)

    with patch(
        "taskrunner.orchestrator._run_fetcher_inline"
    ) as mock_fetch:
        mock_fetch.return_value = '{"temp_f": "72", "condition": "sunny"}'
        result = run_task(task_path, dry_run=True)

    assert "Weather:" in result
    assert "72" in result
    assert "sunny" in result
    assert "Date:" in result


def test_run_task_calls_llm_and_output(tmp_path: Path) -> None:
    """Full run should call fetcher, LLM, and output."""
    task_path = _make_task(tmp_path)

    with (
        patch("taskrunner.orchestrator._run_fetcher_inline") as mock_fetch,
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


def test_gmail_fetcher_through_orchestrator(tmp_path: Path) -> None:
    """Gmail fetcher branch should work through the orchestrator."""
    task = {
        "name": "gmail_test",
        "schedule": "0 8 * * *",
        "fetch": {
            "gmail": {
                "image": "fetcher-gmail:latest",
                "args": {"query": "is:unread", "max_results": "5", "full_body": "false"},
            }
        },
        "prompt": "Date: {date}\nEmails: {gmail}",
        "output": {"type": "stdout", "to": ""},
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
    }
    path = tmp_path / "gmail_test.yaml"
    path.write_text(yaml.dump(task))

    with (
        patch("taskrunner.orchestrator._fetch_gmail_inline") as mock_gmail,
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


def test_fetcher_failure_continues(tmp_path: Path) -> None:
    """If a fetcher fails, the task should continue with an error placeholder."""
    task_path = _make_task(tmp_path)

    with (
        patch("taskrunner.orchestrator._run_fetcher_inline") as mock_fetch,
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
    assert "[Error fetching weather]" in prompt
