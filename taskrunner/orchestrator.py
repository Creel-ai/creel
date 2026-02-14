"""Orchestrator - the core task execution loop.

Reads task definitions, runs executors, calls LLM, routes output.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from taskrunner.llm import run_llm
from taskrunner.models import ExecutorConfig, TaskDefinition, load_task
from taskrunner.outputs import send_output
from taskrunner.secrets import decrypt_env_file

logger = logging.getLogger(__name__)


def run_task(
    task_path: str | Path,
    use_containers: bool = False,
    dry_run: bool = False,
) -> str:
    """Execute a complete task: fetch -> LLM -> output.

    Branches on task.mode:
    - "simple" (default): linear pipeline (fetch -> template -> LLM -> output)
    - "agent": agent loop with tool calling

    Args:
        task_path: Path to the task YAML file.
        use_containers: If True, run executors/LLM in Docker containers.
        dry_run: If True, print the rendered prompt but skip LLM and output.

    Returns:
        The LLM response text (or rendered prompt in dry-run mode).
    """
    task = load_task(task_path)
    logger.info("Running task: %s (mode=%s)", task.name, task.mode)

    # Build context with date info
    now = datetime.now(timezone.utc)
    context: dict[str, str] = {
        "date": now.strftime("%A, %B %d, %Y"),
    }

    # Run each executor and collect results
    for name, executor_config in task.executors.items():
        logger.info("Running executor: %s", name)
        try:
            if use_containers:
                result = _run_executor_container(executor_config)
            else:
                result = _run_executor_inline(name, executor_config)
            context[name] = result
            logger.info("Executor %s completed (%d chars)", name, len(result))
        except Exception as e:
            logger.exception("Executor %s failed", name)
            context[name] = f"[Error fetching {name}: {e}]"

    # Render the prompt template
    prompt = task.prompt.format(**context)

    if dry_run:
        logger.info("Dry run - skipping LLM and output")
        return prompt

    # Prepare LLM secrets if configured
    if task.llm.secrets:
        _load_secrets_to_env(task.llm.secrets)

    if task.mode == "agent":
        result = _run_agent_mode(task, prompt, use_containers)
    else:
        # Simple mode: single LLM call
        logger.info("Calling LLM (%s)", task.llm.model)
        result = run_llm(prompt, task.llm, use_container=use_containers)

    logger.info("LLM response: %d chars", len(result))

    # Route output
    logger.info("Sending output via %s", task.output.type)
    send_output(result, task.output)

    return result


def _run_agent_mode(
    task: TaskDefinition,
    prompt: str,
    use_containers: bool,
) -> str:
    """Run a task in agent mode using the agent loop."""
    from taskrunner.agent import run_agent_loop

    messages = [{"role": "user", "content": prompt}]

    agent_result = run_agent_loop(
        messages=messages,
        llm_config=task.llm,
        tools_config=task.tools,
        agent_config=task.agent,
        use_containers=use_containers,
    )

    logger.info(
        "Agent completed: %d turns, %d tool calls, stop=%s",
        agent_result.turns_used,
        agent_result.tool_calls_made,
        agent_result.stop_reason,
    )

    return agent_result.text


def _run_executor_inline(name: str, config: ExecutorConfig) -> str:
    """Run an executor by importing and calling it directly (no container)."""
    # Load secrets if configured
    env_overrides = {}
    if config.secrets:
        env_overrides = decrypt_env_file(config.secrets)

    import os

    old_env = {}
    for k, v in env_overrides.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        if name == "weather":
            return _exec_weather_inline(config)
        elif name == "calendar":
            return _exec_gcal_inline(config)
        elif name == "gcal_write":
            return _exec_gcal_write_inline(config)
        elif name == "gmail_readonly":
            return _exec_gmail_readonly_inline(config)
        elif name == "gmail_send":
            return _exec_gmail_send_inline(config)
        elif name == "gmail_modify":
            return _exec_gmail_modify_inline(config)
        elif name == "drive":
            return _exec_drive_inline(config)
        elif name == "drive_write":
            return _exec_drive_write_inline(config)
        elif name == "bluebubbles":
            return _exec_bluebubbles_inline(config, "get_recent_messages")
        elif name == "bluebubbles_send":
            return _exec_bluebubbles_inline(config, "send_message")
        elif name == "bluebubbles_react":
            return _exec_bluebubbles_inline(config, "send_reaction")
        elif name == "bluebubbles_chats":
            return _exec_bluebubbles_inline(config, "get_chats")
        elif name == "apple_notes":
            return _exec_apple_notes_inline(config)
        elif name == "apple_reminders":
            return _exec_apple_reminders_inline(config)
        elif name == "brave_search":
            return _exec_brave_search_inline(config)
        elif name == "fetch_url":
            return _exec_fetch_url_inline(config)
        else:
            raise ValueError(f"Unknown inline executor: {name}")
    finally:
        # Restore original env
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _exec_weather_inline(config: ExecutorConfig) -> str:
    """Run weather executor inline."""
    from executors.weather.executor import fetch_weather

    location = config.args.get("location", "Denver")
    result = fetch_weather(location)
    return json.dumps(result, indent=2)


def _exec_gcal_inline(config: ExecutorConfig) -> str:
    """Run Google Calendar executor inline."""
    from executors.gcal.executor import fetch_events

    range_arg = config.args.get("range", "today")
    events = fetch_events(range_arg)
    return json.dumps(events, indent=2)


def _exec_gcal_write_inline(config: ExecutorConfig) -> str:
    """Run Google Calendar write executor inline."""
    from executors.gcal_write.executor import create_event

    summary = config.args.get("summary", "")
    start = config.args.get("start", "")
    end = config.args.get("end", "")
    description = config.args.get("description", "")
    location = config.args.get("location", "")
    event = create_event(summary, start, end, description, location)
    return json.dumps(event, indent=2)


def _exec_gmail_readonly_inline(config: ExecutorConfig) -> str:
    """Run Gmail readonly executor inline (check_email or read_email)."""
    message_id = config.args.get("message_id", "")
    if message_id:
        from executors.gmail_readonly.executor import read_email

        result = read_email(message_id)
        return json.dumps(result, indent=2)

    from executors.gmail_readonly.executor import fetch_emails

    query = config.args.get("query", "is:unread newer_than:1d")
    max_results = int(config.args.get("max_results", 20))
    full_body = str(config.args.get("full_body", "false")).lower() in (
        "true",
        "1",
        "yes",
    )
    emails = fetch_emails(query, max_results, full_body)
    return json.dumps(emails, indent=2)


def _exec_gmail_send_inline(config: ExecutorConfig) -> str:
    """Run Gmail send executor inline."""
    from executors.gmail_send.executor import send_email

    to = config.args.get("to", "")
    subject = config.args.get("subject", "")
    body = config.args.get("body", "")
    result = send_email(to, subject, body)
    return json.dumps(result, indent=2)


def _exec_gmail_modify_inline(config: ExecutorConfig) -> str:
    """Run Gmail modify executor inline."""
    action = config.args.get("action", "")
    message_id = config.args.get("message_id", "")

    if action == "modify":
        from executors.gmail_modify.executor import modify_message

        add_raw = config.args.get("add_labels", "")
        remove_raw = config.args.get("remove_labels", "")
        add_labels = [l.strip() for l in add_raw.split(",") if l.strip()] or None
        remove_labels = (
            [l.strip() for l in remove_raw.split(",") if l.strip()] or None
        )
        result = modify_message(message_id, add_labels, remove_labels)
    elif action == "trash":
        from executors.gmail_modify.executor import trash_message

        result = trash_message(message_id)
    elif action == "delete":
        from executors.gmail_modify.executor import delete_message

        result = delete_message(message_id)
    else:
        raise ValueError(f"gmail_modify: unknown action '{action}' (use modify/trash/delete)")

    return json.dumps(result, indent=2)


def _exec_drive_inline(config: ExecutorConfig) -> str:
    """Run Google Drive executor inline."""
    from executors.drive.executor import list_files

    query = config.args.get("query", "")
    max_results = int(config.args.get("max_results", 20))
    files = list_files(query, max_results)
    return json.dumps(files, indent=2)


def _exec_drive_write_inline(config: ExecutorConfig) -> str:
    """Run Google Drive write executor inline."""
    from executors.drive_write.executor import upload_file

    name = config.args.get("name", "")
    content = config.args.get("content", "")
    mime_type = config.args.get("mime_type", "text/plain")
    folder_id = config.args.get("folder_id", "")
    result = upload_file(name, content, mime_type, folder_id)
    return json.dumps(result, indent=2)


def _exec_bluebubbles_inline(config: ExecutorConfig, action: str) -> str:
    """Run BlueBubbles executor inline."""
    import os
    from executors.bluebubbles.executor import (
        get_chats,
        get_recent_messages,
        send_message,
        send_reaction,
    )

    server_url = os.environ.get("BLUEBUBBLES_URL", "")
    password = os.environ.get("BLUEBUBBLES_PASSWORD", "")
    allowed_recipients = {
        v.strip()
        for v in os.environ.get("ALLOWED_RECIPIENTS", "").split(",")
        if v.strip()
    }
    allowed_chats = {
        v.strip()
        for v in os.environ.get("ALLOWED_CHATS", "").split(",")
        if v.strip()
    }

    if action == "get_recent_messages":
        result = get_recent_messages(
            server_url,
            password,
            allowed_chats,
            chat_id=config.args.get("chat_id") or None,
            limit=int(config.args.get("limit", "20")),
            after_date=config.args.get("after_date") or None,
        )
    elif action == "send_message":
        result = send_message(
            server_url,
            password,
            allowed_recipients,
            chat_id=config.args.get("chat_id", ""),
            text=config.args.get("text", ""),
        )
    elif action == "send_reaction":
        result = send_reaction(
            server_url,
            password,
            allowed_recipients,
            chat_id=config.args.get("chat_id", ""),
            message_guid=config.args.get("message_guid", ""),
            reaction=config.args.get("reaction", ""),
        )
    elif action == "get_chats":
        result = get_chats(
            server_url,
            password,
            allowed_chats,
            limit=int(config.args.get("limit", "20")),
        )
    else:
        raise ValueError(f"Unknown bluebubbles action: {action}")

    return json.dumps(result, indent=2)


def _exec_apple_notes_inline(config: ExecutorConfig) -> str:
    """Run Apple Notes executor inline."""
    action = config.args.get("action", "list_notes")

    if action == "list_notes":
        from executors.apple_notes.executor import list_notes

        folder = config.args.get("folder", "Notes")
        limit = int(config.args.get("limit", "25"))
        result = list_notes(folder, limit)
    elif action == "search_notes":
        from executors.apple_notes.executor import search_notes

        query = config.args.get("query", "")
        result = search_notes(query)
    elif action == "read_note":
        from executors.apple_notes.executor import read_note

        name = config.args.get("name", "")
        result = read_note(name)
    elif action == "create_note":
        from executors.apple_notes.executor import create_note

        title = config.args.get("title", "")
        body = config.args.get("body", "")
        folder = config.args.get("folder", "Notes")
        result = create_note(title, body, folder)
    else:
        raise ValueError(f"Unknown apple_notes action: {action}")

    return json.dumps(result, indent=2)


def _exec_apple_reminders_inline(config: ExecutorConfig) -> str:
    """Run Apple Reminders executor inline."""
    action = config.args.get("action", "list_reminders")

    if action == "list_reminders":
        from executors.apple_reminders.executor import list_reminders

        list_name = config.args.get("list_name", "Reminders")
        result = list_reminders(list_name)
    elif action == "create_reminder":
        from executors.apple_reminders.executor import create_reminder

        title = config.args.get("title", "")
        due_date = config.args.get("due_date") or None
        list_name = config.args.get("list_name", "Reminders")
        notes = config.args.get("notes") or None
        result = create_reminder(title, due_date, list_name, notes)
    elif action == "complete_reminder":
        from executors.apple_reminders.executor import complete_reminder

        name = config.args.get("name", "")
        list_name = config.args.get("list_name", "Reminders")
        result = complete_reminder(name, list_name)
    elif action == "get_lists":
        from executors.apple_reminders.executor import get_lists

        result = get_lists()
    else:
        raise ValueError(f"Unknown apple_reminders action: {action}")

    return json.dumps(result, indent=2)


def _exec_brave_search_inline(config: ExecutorConfig) -> str:
    """Run Brave Search executor inline."""
    from executors.brave_search.executor import search

    query = config.args.get("query", "")
    count = int(config.args.get("count", "5"))
    result = search(query, count)
    return json.dumps(result, indent=2)


def _exec_fetch_url_inline(config: ExecutorConfig) -> str:
    """Run URL fetcher executor inline."""
    from executors.fetch_url.executor import fetch_url

    url = config.args.get("url", "")
    max_chars = int(config.args.get("max_chars", "10000"))
    result = fetch_url(url, max_chars)
    return json.dumps(result, indent=2)


def _ensure_image(image: str) -> None:
    """Build the Docker image if it doesn't already exist.

    Derives the build context from the image name:
      executor-gmail-modify:latest -> executors/gmail_modify/
      llm-runner:latest           -> llm/
    """
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    if result.returncode == 0:
        return

    tag = image.split(":")[0]
    if tag.startswith("executor-"):
        # executor-gmail-modify -> executors/gmail_modify/
        name = tag.removeprefix("executor-").replace("-", "_")
        context = Path("executors") / name
    else:
        # llm-runner -> llm/
        context = Path(tag.replace("-", "_"))
        # Try hyphenated too: llm/ exists as-is
        if not context.exists():
            context = Path(tag)

    if not (context / "Dockerfile").exists():
        raise FileNotFoundError(f"No Dockerfile at {context} for image {image}")

    logger.info("Building image %s from %s", image, context)
    build_result = subprocess.run(
        ["docker", "build", "-t", image, str(context)],
        capture_output=True,
        text=True,
    )
    if build_result.returncode != 0:
        build_err = build_result.stderr.strip() if build_result.stderr else "unknown error"
        logger.error("Docker build failed for %s:\n%s", image, build_err)
        raise RuntimeError(f"Docker build failed for {image}: {build_err[:500]}")


def _run_executor_container(config: ExecutorConfig) -> str:
    """Run an executor in an isolated Docker container.

    Captures both stdout (data) and stderr (logs/errors). Stderr is
    always logged at DEBUG on success and ERROR on failure. The
    request_id is passed into the container as ``CREEL_REQUEST_ID``
    for log correlation.
    """
    from taskrunner.log import request_id_var

    _ensure_image(config.image)
    env_vars: dict[str, str] = {}

    # Decrypt and inject secrets
    if config.secrets:
        env_vars.update(decrypt_env_file(config.secrets))

    # Pass args as env vars
    for key, value in config.args.items():
        env_vars[key.upper()] = value

    # Pass request ID for correlation
    rid = request_id_var.get(None)
    if rid:
        env_vars["CREEL_REQUEST_ID"] = rid

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=True, prefix="creel-"
    ) as env_file:
        for key, value in env_vars.items():
            env_file.write(f"{key}={value}\n")
        env_file.flush()

        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--read-only",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=16M",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--memory=256m",
                    "--cpus=0.5",
                    "--env-file", env_file.name,
                    config.image,
                ],
                capture_output=True,
                text=True,
                timeout=config.timeout,
            )
        except subprocess.TimeoutExpired as e:
            stderr = (e.stderr or "").strip() if isinstance(e.stderr, str) else ""
            if stderr:
                logger.error(
                    "Executor %s stderr (timeout after %ds):\n%s",
                    config.name, config.timeout, stderr,
                )
            raise RuntimeError(
                f"Executor '{config.name}' timed out after {config.timeout}s"
            ) from e

    # Log stderr regardless of exit code
    stderr = result.stderr.strip() if result.stderr else ""
    if stderr:
        if result.returncode == 0:
            logger.debug("Executor %s stderr (success):\n%s", config.name, stderr)
        else:
            logger.error("Executor %s stderr (exit %d):\n%s", config.name, result.returncode, stderr)

    if result.returncode != 0:
        # Include stderr in the error so it propagates to the LLM
        error_detail = stderr[:500] if stderr else f"exit code {result.returncode}"
        raise RuntimeError(
            f"Executor '{config.name}' failed: {error_detail}"
        )

    return result.stdout.strip()


def _load_secrets_to_env(secrets_path: str) -> None:
    """Decrypt a secrets file and load values into the environment."""
    import os

    secrets = decrypt_env_file(secrets_path)
    for key, value in secrets.items():
        os.environ[key] = value
