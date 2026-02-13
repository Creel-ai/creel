"""Orchestrator - the core task execution loop.

Reads task definitions, runs fetchers, calls LLM, routes output.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from taskrunner.llm import run_llm
from taskrunner.models import FetcherConfig, TaskDefinition, load_task
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
        use_containers: If True, run fetchers/LLM in Docker containers.
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

    # Run each fetcher and collect results
    for name, fetcher_config in task.fetch.items():
        logger.info("Running fetcher: %s", name)
        try:
            if use_containers:
                result = _run_fetcher_container(fetcher_config)
            else:
                result = _run_fetcher_inline(name, fetcher_config)
            context[name] = result
            logger.info("Fetcher %s completed (%d chars)", name, len(result))
        except Exception:
            logger.exception("Fetcher %s failed", name)
            context[name] = f"[Error fetching {name}]"

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


def _run_fetcher_inline(name: str, config: FetcherConfig) -> str:
    """Run a fetcher by importing and calling it directly (no container)."""
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
            return _fetch_weather_inline(config)
        elif name == "calendar":
            return _fetch_gcal_inline(config)
        elif name == "gcal_write":
            return _fetch_gcal_write_inline(config)
        elif name == "gmail":
            return _fetch_gmail_inline(config)
        elif name == "gmail_read":
            return _fetch_gmail_read_inline(config)
        elif name == "gmail_send":
            return _fetch_gmail_send_inline(config)
        elif name == "gmail_modify":
            return _fetch_gmail_modify_inline(config)
        elif name == "drive":
            return _fetch_drive_inline(config)
        elif name == "drive_write":
            return _fetch_drive_write_inline(config)
        else:
            raise ValueError(f"Unknown inline fetcher: {name}")
    finally:
        # Restore original env
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fetch_weather_inline(config: FetcherConfig) -> str:
    """Run weather fetcher inline."""
    from fetchers.weather.fetcher import fetch_weather

    location = config.args.get("location", "Denver")
    result = fetch_weather(location)
    return json.dumps(result, indent=2)


def _fetch_gcal_inline(config: FetcherConfig) -> str:
    """Run Google Calendar fetcher inline."""
    from fetchers.gcal.fetcher import fetch_events

    range_arg = config.args.get("range", "today")
    events = fetch_events(range_arg)
    return json.dumps(events, indent=2)


def _fetch_gcal_write_inline(config: FetcherConfig) -> str:
    """Run Google Calendar write fetcher inline."""
    from fetchers.gcal_write.fetcher import create_event

    summary = config.args.get("summary", "")
    start = config.args.get("start", "")
    end = config.args.get("end", "")
    description = config.args.get("description", "")
    location = config.args.get("location", "")
    event = create_event(summary, start, end, description, location)
    return json.dumps(event, indent=2)


def _fetch_gmail_inline(config: FetcherConfig) -> str:
    """Run Gmail fetcher inline."""
    from fetchers.gmail.fetcher import fetch_emails

    query = config.args.get("query", "is:unread newer_than:1d")
    max_results = int(config.args.get("max_results", 20))
    full_body = str(config.args.get("full_body", "false")).lower() in (
        "true",
        "1",
        "yes",
    )
    emails = fetch_emails(query, max_results, full_body)
    return json.dumps(emails, indent=2)


def _fetch_gmail_read_inline(config: FetcherConfig) -> str:
    """Run Gmail read fetcher inline."""
    from fetchers.gmail.fetcher import read_email

    message_id = config.args.get("message_id", "")
    result = read_email(message_id)
    return json.dumps(result, indent=2)


def _fetch_gmail_send_inline(config: FetcherConfig) -> str:
    """Run Gmail send fetcher inline."""
    from fetchers.gmail_send.fetcher import send_email

    to = config.args.get("to", "")
    subject = config.args.get("subject", "")
    body = config.args.get("body", "")
    result = send_email(to, subject, body)
    return json.dumps(result, indent=2)


def _fetch_gmail_modify_inline(config: FetcherConfig) -> str:
    """Run Gmail modify fetcher inline."""
    action = config.args.get("action", "")
    message_id = config.args.get("message_id", "")

    if action == "modify":
        from fetchers.gmail_modify.fetcher import modify_message

        add_raw = config.args.get("add_labels", "")
        remove_raw = config.args.get("remove_labels", "")
        add_labels = [l.strip() for l in add_raw.split(",") if l.strip()] or None
        remove_labels = (
            [l.strip() for l in remove_raw.split(",") if l.strip()] or None
        )
        result = modify_message(message_id, add_labels, remove_labels)
    elif action == "trash":
        from fetchers.gmail_modify.fetcher import trash_message

        result = trash_message(message_id)
    elif action == "delete":
        from fetchers.gmail_modify.fetcher import delete_message

        result = delete_message(message_id)
    else:
        raise ValueError(f"gmail_modify: unknown action '{action}' (use modify/trash/delete)")

    return json.dumps(result, indent=2)


def _fetch_drive_inline(config: FetcherConfig) -> str:
    """Run Google Drive fetcher inline."""
    from fetchers.drive.fetcher import list_files

    query = config.args.get("query", "")
    max_results = int(config.args.get("max_results", 20))
    files = list_files(query, max_results)
    return json.dumps(files, indent=2)


def _fetch_drive_write_inline(config: FetcherConfig) -> str:
    """Run Google Drive write fetcher inline."""
    from fetchers.drive_write.fetcher import upload_file

    name = config.args.get("name", "")
    content = config.args.get("content", "")
    mime_type = config.args.get("mime_type", "text/plain")
    folder_id = config.args.get("folder_id", "")
    result = upload_file(name, content, mime_type, folder_id)
    return json.dumps(result, indent=2)


def _ensure_image(image: str) -> None:
    """Build the Docker image if it doesn't already exist.

    Derives the build context from the image name:
      fetcher-gmail-modify:latest -> fetchers/gmail_modify/
      llm-runner:latest           -> llm/
    """
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    if result.returncode == 0:
        return

    tag = image.split(":")[0]
    if tag.startswith("fetcher-"):
        # fetcher-gmail-modify -> fetchers/gmail_modify/
        name = tag.removeprefix("fetcher-").replace("-", "_")
        context = Path("fetchers") / name
    else:
        # llm-runner -> llm/
        context = Path(tag.replace("-", "_"))
        # Try hyphenated too: llm/ exists as-is
        if not context.exists():
            context = Path(tag)

    if not (context / "Dockerfile").exists():
        raise FileNotFoundError(f"No Dockerfile at {context} for image {image}")

    logger.info("Building image %s from %s", image, context)
    subprocess.run(
        ["docker", "build", "-t", image, str(context)],
        check=True,
    )


def _run_fetcher_container(config: FetcherConfig) -> str:
    """Run a fetcher in an isolated Docker container."""
    _ensure_image(config.image)
    env_flags: list[str] = []

    # Decrypt and inject secrets
    if config.secrets:
        secrets = decrypt_env_file(config.secrets)
        for key, value in secrets.items():
            env_flags.extend(["-e", f"{key}={value}"])

    # Pass args as env vars
    for key, value in config.args.items():
        env_flags.extend(["-e", f"{key.upper()}={value}"])

    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16M",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=256m",
            "--cpus=0.5",
            *env_flags,
            config.image,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def _load_secrets_to_env(secrets_path: str) -> None:
    """Decrypt a secrets file and load values into the environment."""
    import os

    secrets = decrypt_env_file(secrets_path)
    for key, value in secrets.items():
        os.environ[key] = value
