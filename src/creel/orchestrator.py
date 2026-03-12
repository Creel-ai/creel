"""Orchestrator - the core task execution loop.

Reads task definitions, runs executors, calls LLM, routes output.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from creel.containers import (
    ImageBuildCache,
    _compute_executor_hash,
    _ensure_image,
    _image_cache,
    _run_executor_container,
    collect_required_images,
    prebuild_images,
)
from creel.llm import run_llm
from creel.models import ExecutorConfig, TaskDefinition, load_task
from creel.outputs import send_output
from creel.secrets import decrypt_env_file

if TYPE_CHECKING:
    from creel.models import AgentDefinition

logger = logging.getLogger(__name__)

_GOOGLE_TOKEN_MAX_AGE_SECONDS = 3600

# Lock for inline executor env-var mutations (os.environ is process-global)
_ENV_LOCK = threading.Lock()

# Re-export container symbols for backward compatibility
__all__ = [
    "ImageBuildCache",
    "_compute_executor_hash",
    "_ensure_image",
    "_image_cache",
    "_run_executor_container",
    "collect_required_images",
    "prebuild_images",
]


_EXECUTOR_TO_BRIDGE_SCOPE: dict[str, str] = {
    "apple_notes": "NOTES",
    "apple_reminders": "REMINDERS",
    "things": "THINGS",
    "imessage_bridge": "IMESSAGE",
    "browser": "BROWSER",
    "git_ops": "GIT",
}


def _replace_google_credentials_with_access_token(env_vars: dict[str, str]) -> None:
    """Replace refresh-token JSON with a short-lived access token."""
    creds_json = env_vars.pop("GOOGLE_CREDENTIALS_JSON", None)
    if not creds_json:
        return

    from creel.oauth import get_google_access_token_from_json

    cache_key = f"google_creds:{sha256(creds_json.encode('utf-8')).hexdigest()}"
    try:
        env_vars["GOOGLE_ACCESS_TOKEN"] = get_google_access_token_from_json(
            creds_json,
            cache_key=cache_key,
            max_token_age_seconds=_GOOGLE_TOKEN_MAX_AGE_SECONDS,
            force_refresh=False,
        )
    except Exception:
        logger.exception(
            "Failed to mint Google access token; executor will not receive credentials"
        )
        raise


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
    now = datetime.now(UTC)
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
    messages = [{"role": "user", "content": prompt}]

    if use_containers:
        from creel.container_agent import run_agent_loop_container

        agent_result = run_agent_loop_container(
            messages=messages,
            llm_config=task.llm,
            tools_config=task.tools,
            agent_config=task.agent,
            use_containers=use_containers,
        )
    else:
        from creel.agent import run_agent_loop

        agent_result = run_agent_loop(
            messages=messages,
            llm_config=task.llm,
            tools_config=task.tools,
            agent_config=task.agent,
            use_containers=use_containers,
            allowed_tools=task.allowed_tools or None,
        )

    logger.info(
        "Agent completed: %d turns, %d tool calls, stop=%s",
        agent_result.turns_used,
        agent_result.tool_calls_made,
        agent_result.stop_reason,
    )

    return agent_result.text


@contextlib.contextmanager
def _env_override(overrides: dict[str, str]) -> Generator[None, None, None]:
    """Temporarily set environment variables, restoring originals on exit.

    Acquires _ENV_LOCK to prevent concurrent env mutations from interleaving.
    Use this instead of manually saving/restoring os.environ entries.
    """
    saved: dict[str, str | None] = {}
    with _ENV_LOCK:
        for key, value in overrides.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            yield
        finally:
            for key, prev in saved.items():
                if prev is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prev


def _run_executor_inline(name: str, config: ExecutorConfig) -> str:
    """Run an executor by importing and calling it directly (no container).

    Uses _env_override to temporarily set secrets and bridge env vars,
    restoring the original values when done.
    """
    # Load secrets if configured
    env_overrides: dict[str, str] = {}
    if config.secrets:
        env_overrides = decrypt_env_file(config.secrets)
        _replace_google_credentials_with_access_token(env_overrides)

    # Inject BRIDGE_URL and scoped BRIDGE_TOKEN for bridge-calling executors
    if "BRIDGE_URL" not in env_overrides and not os.environ.get("BRIDGE_URL"):
        env_overrides["BRIDGE_URL"] = os.environ.get("CREEL_BRIDGE_URL", "http://localhost:8099")
    if "BRIDGE_TOKEN" not in env_overrides and not os.environ.get("BRIDGE_TOKEN"):
        scope_name = _EXECUTOR_TO_BRIDGE_SCOPE.get(name, name.upper())
        scoped_token = os.environ.get(f"BRIDGE_TOKEN_{scope_name}", "")
        if scoped_token:
            env_overrides["BRIDGE_TOKEN"] = scoped_token

    with _env_override(env_overrides):
        return _dispatch_executor(name, config)


def _dispatch_executor(name: str, config: ExecutorConfig) -> str:
    """Dispatch to the correct inline executor handler."""
    # Dict built at call time so unittest.mock patches are picked up.
    dispatch: dict[str, Callable[[ExecutorConfig], str]] = {
        "weather": _exec_weather_inline,
        "calendar": _exec_gcal_inline,
        "gcal_write": _exec_gcal_write_inline,
        "gmail_readonly": _exec_gmail_readonly_inline,
        "gmail_send": _exec_gmail_send_inline,
        "gmail_modify": _exec_gmail_modify_inline,
        "drive": _exec_drive_inline,
        "drive_write": _exec_drive_write_inline,
        "google_docs": _exec_google_docs_inline,
        "google_sheets": _exec_google_sheets_inline,
        "google_slides": _exec_google_slides_inline,
        "apple_notes": _exec_apple_notes_inline,
        "apple_reminders": _exec_apple_reminders_inline,
        "brave_search": _exec_brave_search_inline,
        "notion": _exec_notion_inline,
        "notion_write": _exec_notion_write_inline,
        "fetch_url": _exec_fetch_url_inline,
        "browser": _exec_browser_inline,
        "exec": _exec_exec_inline,
        "file_ops": _exec_file_ops_inline,
        "github": _exec_github_inline,
        "coding": _exec_coding_inline,
    }

    # BlueBubbles variants share one handler with different actions.
    bluebubbles_dispatch: dict[str, str] = {
        "bluebubbles": "get_recent_messages",
        "bluebubbles_send": "send_message",
        "bluebubbles_react": "send_reaction",
        "bluebubbles_chats": "get_chats",
    }

    if name in bluebubbles_dispatch:
        return _exec_bluebubbles_inline(config, bluebubbles_dispatch[name])

    handler = dispatch.get(name)
    if handler is None:
        raise ValueError(f"Unknown inline executor: {name}")
    return handler(config)


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
        add_labels = [label.strip() for label in add_raw.split(",") if label.strip()] or None
        remove_labels = [label.strip() for label in remove_raw.split(",") if label.strip()] or None
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


def _exec_google_docs_inline(config: ExecutorConfig) -> str:
    """Run Google Docs executor inline."""
    action = config.args.get("action", "")

    if action == "read":
        from executors.google_docs.executor import read_document

        document_id = config.args.get("document_id", "")
        result = read_document(document_id)
    elif action == "create":
        from executors.google_docs.executor import create_document

        title = config.args.get("title", "")
        body = config.args.get("body", "")
        result = create_document(title, body)
    elif action == "append":
        from executors.google_docs.executor import append_text

        document_id = config.args.get("document_id", "")
        text = config.args.get("text", "")
        result = append_text(document_id, text)
    elif action == "replace":
        from executors.google_docs.executor import replace_text

        document_id = config.args.get("document_id", "")
        find = config.args.get("find", "")
        replace_with = config.args.get("replace_with", "")
        match_case = str(config.args.get("match_case", "true")).lower() in ("true", "1", "yes")
        result = replace_text(document_id, find, replace_with, match_case)
    elif action == "insert":
        from executors.google_docs.executor import insert_text

        document_id = config.args.get("document_id", "")
        text = config.args.get("text", "")
        index = int(config.args.get("index", "1"))
        result = insert_text(document_id, text, index)
    else:
        raise ValueError(
            f"google_docs: unknown action '{action}' (use read/create/append/replace/insert)"
        )

    return json.dumps(result, indent=2)


def _exec_google_sheets_inline(config: ExecutorConfig) -> str:
    """Run Google Sheets executor inline."""
    action = config.args.get("action", "")

    if action == "read":
        from executors.google_sheets.executor import read_sheet

        spreadsheet_id = config.args.get("spreadsheet_id", "")
        range_ = config.args.get("range", "")
        result = read_sheet(spreadsheet_id, range_)
    elif action == "create":
        from executors.google_sheets.executor import create_spreadsheet

        title = config.args.get("title", "")
        sheet_name = config.args.get("sheet_name", "")
        data = config.args.get("data", "")
        result = create_spreadsheet(title, sheet_name, data)
    elif action == "write":
        from executors.google_sheets.executor import write_to_sheet

        spreadsheet_id = config.args.get("spreadsheet_id", "")
        range_ = config.args.get("range", "")
        data = config.args.get("data", "")
        value_input_option = config.args.get("value_input_option", "USER_ENTERED")
        result = write_to_sheet(spreadsheet_id, range_, data, value_input_option)
    elif action == "append":
        from executors.google_sheets.executor import append_to_sheet

        spreadsheet_id = config.args.get("spreadsheet_id", "")
        range_ = config.args.get("range", "")
        data = config.args.get("data", "")
        value_input_option = config.args.get("value_input_option", "USER_ENTERED")
        result = append_to_sheet(spreadsheet_id, range_, data, value_input_option)
    else:
        raise ValueError(f"google_sheets: unknown action '{action}' (use read/create/write/append)")

    return json.dumps(result, indent=2)


def _exec_google_slides_inline(config: ExecutorConfig) -> str:
    """Run Google Slides executor inline."""
    action = config.args.get("action", "")

    if action == "read":
        from executors.google_slides.executor import read_presentation

        presentation_id = config.args.get("presentation_id", "")
        result = read_presentation(presentation_id)
    elif action == "create":
        from executors.google_slides.executor import create_presentation

        title = config.args.get("title", "")
        result = create_presentation(title)
    elif action == "add_slide":
        from executors.google_slides.executor import add_slide

        presentation_id = config.args.get("presentation_id", "")
        title = config.args.get("title", "")
        body = config.args.get("body", "")
        layout = config.args.get("layout", "BLANK")
        result = add_slide(presentation_id, title, body, layout)
    elif action == "replace_text":
        from executors.google_slides.executor import replace_text

        presentation_id = config.args.get("presentation_id", "")
        find = config.args.get("find", "")
        replace_with = config.args.get("replace_with", "")
        match_case = str(config.args.get("match_case", "true")).lower() in ("true", "1", "yes")
        result = replace_text(presentation_id, find, replace_with, match_case)
    else:
        raise ValueError(
            f"google_slides: unknown action '{action}' (use read/create/add_slide/replace_text)"
        )

    return json.dumps(result, indent=2)


def _exec_bluebubbles_inline(config: ExecutorConfig, action: str) -> str:
    """Run BlueBubbles executor inline."""
    from executors.bluebubbles.executor import (
        get_chats,
        get_recent_messages,
        send_message,
        send_reaction,
    )

    server_url = os.environ.get("BLUEBUBBLES_URL", "")
    password = os.environ.get("BLUEBUBBLES_PASSWORD", "")
    allowed_recipients = {
        v.strip() for v in os.environ.get("ALLOWED_RECIPIENTS", "").split(",") if v.strip()
    }
    allowed_chats = {v.strip() for v in os.environ.get("ALLOWED_CHATS", "").split(",") if v.strip()}

    result: object
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
    """Run Apple Notes executor inline via bridge."""
    from executors.apple_notes.executor import main as apple_notes_main

    env_vars = {
        k: v
        for k, v in {
            "ACTION": config.args.get("action", "list"),
            "FOLDER": config.args.get("folder", ""),
            "QUERY": config.args.get("query", ""),
            "TITLE": config.args.get("title", ""),
            "BODY": config.args.get("body", ""),
        }.items()
        if v  # Only set non-empty values
    }

    old_stdout = sys.stdout
    try:
        sys.stdout = captured_output = StringIO()
        with _env_override(env_vars):
            apple_notes_main()
        return captured_output.getvalue().strip() or "{}"
    finally:
        sys.stdout = old_stdout


def _exec_apple_reminders_inline(config: ExecutorConfig) -> str:
    """Run Apple Reminders executor inline via bridge."""
    from executors.apple_reminders.executor import main as apple_reminders_main

    env_vars = {
        k: v
        for k, v in {
            "ACTION": config.args.get("action", "list"),
            "FILTER": config.args.get("filter", "all"),
            "TITLE": config.args.get("title", ""),
            "LIST": config.args.get("list_name", ""),
            "DUE": config.args.get("due_date", ""),
            "ID": config.args.get("id", ""),
        }.items()
        if v
    }

    old_stdout = sys.stdout
    try:
        sys.stdout = captured_output = StringIO()
        with _env_override(env_vars):
            apple_reminders_main()
        return captured_output.getvalue().strip() or "{}"
    finally:
        sys.stdout = old_stdout


def _exec_brave_search_inline(config: ExecutorConfig) -> str:
    """Run Brave Search executor inline."""
    from executors.brave_search.executor import search

    query = config.args.get("query", "")
    count = int(config.args.get("count", "5"))
    result = search(query, count)
    return json.dumps(result, indent=2)


def _exec_notion_inline(config: ExecutorConfig) -> str:
    """Run Notion executor inline."""
    from executors.notion.executor import run_action

    action = config.args.get("action", "")
    if not action:
        raise ValueError("notion executor requires an 'action' argument")

    result = run_action(
        action=action,
        query=config.args.get("query", ""),
        page_id=config.args.get("page_id", ""),
        database_id=config.args.get("database_id", ""),
        filter_json=config.args.get("filter_json", ""),
        sorts_json=config.args.get("sorts_json", ""),
        page_size=config.args.get("page_size"),
        start_cursor=config.args.get("start_cursor", ""),
    )
    return json.dumps(result, indent=2)


def _exec_notion_write_inline(config: ExecutorConfig) -> str:
    """Run Notion write executor inline."""
    from executors.notion_write.executor import run_action

    action = config.args.get("action", "")
    if not action:
        raise ValueError("notion_write executor requires an 'action' argument")

    result = run_action(
        action=action,
        page_id=config.args.get("page_id", ""),
        database_id=config.args.get("database_id", ""),
        properties_json=config.args.get("properties_json", ""),
        children_json=config.args.get("children_json", ""),
    )
    return json.dumps(result, indent=2)


def _exec_fetch_url_inline(config: ExecutorConfig) -> str:
    """Run URL fetcher executor inline."""
    from executors.fetch_url.executor import fetch_url

    url = config.args.get("url", "")
    max_chars = int(config.args.get("max_chars", "10000"))
    result = fetch_url(url, max_chars)
    return json.dumps(result, indent=2)


def _exec_browser_inline(config: ExecutorConfig) -> str:
    """Run browser executor inline by calling library functions directly."""
    from executors.browser.executor import (
        click,
        close_session,
        connect,
        get_content,
        get_links,
        navigate,
        screenshot,
        sessions,
        type_text,
    )

    action = config.args.get("action", "connect")

    if action == "connect":
        mode = config.args.get("mode", "managed")
        cdp_url = config.args.get("cdp_url") or None
        headless = str(config.args.get("headless", "true")).lower() in ("true", "1", "yes")
        result = connect(mode=mode, cdp_url=cdp_url, headless=headless)
    elif action == "navigate":
        session_id = config.args.get("session_id", "")
        url = config.args.get("url", "")
        result = navigate(session_id, url)
    elif action == "content":
        session_id = config.args.get("session_id", "")
        selector = config.args.get("selector") or None
        result = get_content(session_id, selector)
    elif action == "click":
        session_id = config.args.get("session_id", "")
        selector = config.args.get("selector", "")
        result = click(session_id, selector)
    elif action == "type":
        session_id = config.args.get("session_id", "")
        selector = config.args.get("selector", "")
        text = config.args.get("text", "")
        result = type_text(session_id, selector, text)
    elif action == "screenshot":
        session_id = config.args.get("session_id", "")
        full_page = str(config.args.get("full_page", "false")).lower() in ("true", "1", "yes")
        result = screenshot(session_id, full_page=full_page)
    elif action == "links":
        session_id = config.args.get("session_id", "")
        result = get_links(session_id)
    elif action == "close":
        session_id = config.args.get("session_id", "")
        result = close_session(session_id)
    elif action == "sessions":
        result = sessions()
    else:
        raise ValueError(f"Unknown browser action: {action}")

    return json.dumps(result, indent=2)


def _exec_exec_inline(config: ExecutorConfig) -> str:
    """Run exec executor inline."""
    from executors.exec.executor import run_command

    command = config.args.get("command", "")
    workdir = config.args.get("workdir")

    if not command:
        raise ValueError("exec executor requires a 'command' argument")

    result = run_command(command, workdir)
    return json.dumps(result, indent=2)


def _exec_file_ops_inline(config: ExecutorConfig) -> str:
    """Run file_ops executor inline."""
    from executors.file_ops.executor import ACTIONS

    action = config.args.get("action", "")
    if action not in ACTIONS:
        raise ValueError(f"file_ops: unknown action '{action}'")

    # Map config args to the uppercase env vars the executor expects
    env_map = {
        "workspace": "WORKSPACE",
        "action": "ACTION",
        "file_path": "FILE_PATH",
        "content": "CONTENT",
        "old_text": "OLD_TEXT",
        "new_text": "NEW_TEXT",
        "offset": "OFFSET",
        "limit": "LIMIT",
        "directory": "DIRECTORY",
        "pattern": "PATTERN",
        "recursive": "RECURSIVE",
    }

    env_vars = {
        env_key: str(config.args[arg_key])
        for arg_key, env_key in env_map.items()
        if config.args.get(arg_key, "")
    }

    with _env_override(env_vars):
        result = ACTIONS[action]()
    return json.dumps(result, indent=2)


def _exec_github_inline(config: ExecutorConfig) -> str:
    """Run github executor inline."""
    from executors.github.executor import run_gh_command

    command = config.args.get("command", "")
    repo = config.args.get("repo") or None

    if not command:
        raise ValueError("github executor requires a 'command' argument")

    result = run_gh_command(command, repo)
    return json.dumps(result, indent=2)


def _exec_coding_inline(config: ExecutorConfig) -> str:
    """Run coding executor inline."""
    from executors.coding.executor import run_command

    command = config.args.get("command", "")
    workdir = config.args.get("workdir") or None
    mount = config.args.get("mount") or None
    timeout_str = config.args.get("timeout") or None

    if not command:
        raise ValueError("coding executor requires a 'command' argument")

    timeout = None
    if timeout_str:
        try:
            timeout = int(timeout_str)
        except ValueError:
            pass

    result = run_command(command, workdir=workdir, mount=mount, timeout=timeout)
    return json.dumps(result, indent=2)


def _load_secrets_to_env(secrets_path: str) -> None:
    """Decrypt a secrets file and load values into the environment."""
    secrets = decrypt_env_file(secrets_path)
    for key, value in secrets.items():
        os.environ[key] = value
