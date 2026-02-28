"""Orchestrator - the core task execution loop.

Reads task definitions, runs executors, calls LLM, routes output.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from taskrunner.llm import run_llm
from taskrunner.models import (
    BridgeConfig,
    ExecutorConfig,
    TaskDefinition,
    ToolConfig,
    load_task,
)
from taskrunner.outputs import send_output
from taskrunner.secrets import decrypt_env_file

if TYPE_CHECKING:
    from taskrunner.models import AgentDefinition

logger = logging.getLogger(__name__)

_GOOGLE_TOKEN_MAX_AGE_SECONDS = 3600

# Lock for inline executor env-var mutations (os.environ is process-global)
_ENV_LOCK = threading.Lock()

_HASH_GLOBS = ("Dockerfile", "**/*.py", "**/*.txt")


class ImageBuildCache:
    """Coordinates Docker image builds, deduplicating concurrent requests.

    The first thread to request a given image "claims" the build; other
    threads wait on a ``threading.Event`` until the build finishes.  On
    failure the error is stored but cleared on the next access so that
    retries are possible (handles transient Docker daemon failures).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (event, result_or_none, error_or_none)
        self._builds: dict[
            str, tuple[threading.Event, str | None, Exception | None]
        ] = {}

    def ensure_image(self, image: str) -> str:
        """Build (or wait for) a Docker image.  Returns the usable image ref."""
        key = self._cache_key(image)
        event: threading.Event | None = None
        claimed = False

        with self._lock:
            if key in self._builds:
                ev, result, error = self._builds[key]
                if ev.is_set():
                    if error is None:
                        return result  # type: ignore[return-value]
                    # Previous build failed — clear so we can retry
                    del self._builds[key]
                else:
                    # Another thread is building; we'll wait outside the lock
                    event = ev

            if event is None and key not in self._builds:
                # Claim the build
                event = threading.Event()
                self._builds[key] = (event, None, None)
                claimed = True

        if claimed:
            assert event is not None
            try:
                result = _ensure_image_uncached(image)
                with self._lock:
                    self._builds[key] = (event, result, None)
                event.set()
                return result
            except Exception as exc:
                with self._lock:
                    self._builds[key] = (event, None, exc)
                event.set()
                raise

        # Wait for another thread's in-progress build
        assert event is not None
        event.wait()
        with self._lock:
            entry = self._builds.get(key)
            if entry is None or entry[0] is not event:
                # Our build was superseded by a retry; re-enter from the top
                superseded = True
            else:
                superseded = False
                _, result, error = entry
        if superseded:
            return self.ensure_image(image)
        if error is not None:
            raise RuntimeError(str(error)) from error
        return result  # type: ignore[return-value]

    def start_prebuild(self, images: list[str]) -> list[threading.Thread]:
        """Spawn daemon threads to pre-build each image."""
        threads = []
        for img in images:
            t = threading.Thread(
                target=self._prebuild_one,
                args=(img,),
                daemon=True,
                name=f"prebuild-{img}",
            )
            t.start()
            threads.append(t)
        return threads

    def _prebuild_one(self, image: str) -> None:
        try:
            self.ensure_image(image)
        except Exception:
            logger.warning("Pre-build failed for %s (will retry on demand)", image)

    def clear(self) -> None:
        """Reset the cache (primarily for testing)."""
        with self._lock:
            self._builds.clear()

    @staticmethod
    def _cache_key(image: str) -> str:
        """Derive the cache key.

        Executor images share a key by base name (``executor-weather``)
        since the content-hash tag is computed inside the build.  Other
        images use their full name.
        """
        base = image.split(":")[0]
        if base.startswith("executor-"):
            # Content hash is computed inside the build, so different tags
            # (e.g. :latest vs :abc123) map to the same build work.
            return base
        return image


_image_cache = ImageBuildCache()


def _compute_executor_hash(executor_dir: Path) -> str:
    """Hash all source files in an executor directory and shared context files.

    Returns the first 12 hex chars of the SHA-256 digest computed over
    sorted (relative-path, file-contents) pairs.  Shared files in the
    parent build-context directory (e.g. ``google_creds.py``) are also
    included so that changes to shared modules trigger a rebuild.
    """
    h = sha256()
    # Executor-specific files
    paths = sorted(
        p for pattern in _HASH_GLOBS for p in executor_dir.glob(pattern) if p.is_file()
    )
    # Shared files in the build context (src/executors/)
    context_dir = executor_dir.parent
    shared = sorted(
        p for pattern in _HASH_GLOBS for p in context_dir.glob(pattern) if p.is_file()
    )
    for p in paths:
        h.update(p.relative_to(executor_dir).as_posix().encode())
        h.update(p.read_bytes())
    for p in shared:
        h.update(("../" + p.relative_to(context_dir).as_posix()).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


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

    from taskrunner.oauth import get_google_access_token_from_json

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
    messages = [{"role": "user", "content": prompt}]

    if use_containers:
        from taskrunner.container_agent import run_agent_loop_container

        agent_result = run_agent_loop_container(
            messages=messages,
            llm_config=task.llm,
            tools_config=task.tools,
            agent_config=task.agent,
            use_containers=use_containers,
        )
    else:
        from taskrunner.agent import run_agent_loop

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


def _run_executor_inline(name: str, config: ExecutorConfig) -> str:
    """Run an executor by importing and calling it directly (no container).

    Uses _ENV_LOCK to serialize env-var mutations since os.environ is
    process-global and concurrent calls could interleave.
    """
    # Load secrets if configured
    env_overrides = {}
    if config.secrets:
        env_overrides = decrypt_env_file(config.secrets)
        _replace_google_credentials_with_access_token(env_overrides)

    with _ENV_LOCK:
        return _run_executor_inline_locked(name, config, env_overrides)


def _run_executor_inline_locked(
    name: str,
    config: ExecutorConfig,
    env_overrides: dict[str, str],
) -> str:
    """Inner executor dispatch, called under _ENV_LOCK."""
    import os

    old_env: dict[str, str | None] = {}
    for k, v in env_overrides.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    # Inject BRIDGE_URL and scoped BRIDGE_TOKEN for bridge-calling executors
    # in inline mode (container mode handles this in _build_container_env).
    if not os.environ.get("BRIDGE_URL"):
        # Bridge URL defaults to localhost:8099 for inline execution
        bridge_url = os.environ.get("CREEL_BRIDGE_URL", "http://localhost:8099")
        old_env.setdefault("BRIDGE_URL", os.environ.get("BRIDGE_URL"))
        os.environ["BRIDGE_URL"] = bridge_url
    if not os.environ.get("BRIDGE_TOKEN"):
        scope_name = _EXECUTOR_TO_BRIDGE_SCOPE.get(name, name.upper())
        scoped_token = os.environ.get(f"BRIDGE_TOKEN_{scope_name}", "")
        if scoped_token:
            old_env.setdefault("BRIDGE_TOKEN", os.environ.get("BRIDGE_TOKEN"))
            os.environ["BRIDGE_TOKEN"] = scoped_token

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
        elif name == "google_docs":
            return _exec_google_docs_inline(config)
        elif name == "google_sheets":
            return _exec_google_sheets_inline(config)
        elif name == "google_slides":
            return _exec_google_slides_inline(config)
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
        elif name == "notion":
            return _exec_notion_inline(config)
        elif name == "notion_write":
            return _exec_notion_write_inline(config)
        elif name == "fetch_url":
            return _exec_fetch_url_inline(config)
        elif name == "browser":
            return _exec_browser_inline(config)
        elif name == "exec":
            return _exec_exec_inline(config)
        elif name == "file_ops":
            return _exec_file_ops_inline(config)
        else:
            raise ValueError(f"Unknown inline executor: {name}")
    finally:
        # Restore original env
        for k, old_value in old_env.items():
            if old_value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_value


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
        add_labels = [
            label.strip() for label in add_raw.split(",") if label.strip()
        ] or None
        remove_labels = [
            label.strip() for label in remove_raw.split(",") if label.strip()
        ] or None
        result = modify_message(message_id, add_labels, remove_labels)
    elif action == "trash":
        from executors.gmail_modify.executor import trash_message

        result = trash_message(message_id)
    elif action == "delete":
        from executors.gmail_modify.executor import delete_message

        result = delete_message(message_id)
    else:
        raise ValueError(
            f"gmail_modify: unknown action '{action}' (use modify/trash/delete)"
        )

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
        match_case = str(config.args.get("match_case", "true")).lower() in (
            "true",
            "1",
            "yes",
        )
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
        raise ValueError(
            f"google_sheets: unknown action '{action}' (use read/create/write/append)"
        )

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
        match_case = str(config.args.get("match_case", "true")).lower() in (
            "true",
            "1",
            "yes",
        )
        result = replace_text(presentation_id, find, replace_with, match_case)
    else:
        raise ValueError(
            f"google_slides: unknown action '{action}' (use read/create/add_slide/replace_text)"
        )

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
        v.strip() for v in os.environ.get("ALLOWED_CHATS", "").split(",") if v.strip()
    }

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
    import os

    from executors.apple_notes.executor import main as apple_notes_main

    # Set environment variables for the bridge-calling executor
    old_env: dict[str, str | None] = {}
    env_vars = {
        "ACTION": config.args.get("action", "list"),
        "FOLDER": config.args.get("folder", ""),
        "QUERY": config.args.get("query", ""),
        "TITLE": config.args.get("title", ""),
        "BODY": config.args.get("body", ""),
    }

    for key, value in env_vars.items():
        if value:  # Only set non-empty values
            old_env[key] = os.environ.get(key)
            os.environ[key] = str(value)

    try:
        # Capture stdout from the bridge executor
        import sys
        from io import StringIO

        old_stdout = sys.stdout
        sys.stdout = captured_output = StringIO()

        apple_notes_main()

        result = captured_output.getvalue()
        return result.strip() or "{}"

    finally:
        # Restore environment
        sys.stdout = old_stdout
        for key in env_vars:
            old_value = old_env.get(key)
            if old_value is not None:
                os.environ[key] = old_value
            else:
                os.environ.pop(key, None)


def _exec_apple_reminders_inline(config: ExecutorConfig) -> str:
    """Run Apple Reminders executor inline via bridge."""
    import os

    from executors.apple_reminders.executor import main as apple_reminders_main

    # Set environment variables for the bridge-calling executor
    old_env: dict[str, str | None] = {}
    env_vars = {
        "ACTION": config.args.get("action", "list"),
        "FILTER": config.args.get("filter", "all"),
        "TITLE": config.args.get("title", ""),
        "LIST": config.args.get("list_name", ""),
        "DUE": config.args.get("due_date", ""),
        "ID": config.args.get("id", ""),
    }

    for key, value in env_vars.items():
        if value:  # Only set non-empty values
            old_env[key] = os.environ.get(key)
            os.environ[key] = str(value)

    try:
        # Capture stdout from the bridge executor
        import sys
        from io import StringIO

        old_stdout = sys.stdout
        sys.stdout = captured_output = StringIO()

        apple_reminders_main()

        result = captured_output.getvalue()
        return result.strip() or "{}"

    finally:
        # Restore environment
        sys.stdout = old_stdout
        for key in env_vars:
            old_value = old_env.get(key)
            if old_value is not None:
                os.environ[key] = old_value
            else:
                os.environ.pop(key, None)


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
        headless = str(config.args.get("headless", "true")).lower() in (
            "true",
            "1",
            "yes",
        )
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
        full_page = str(config.args.get("full_page", "false")).lower() in (
            "true",
            "1",
            "yes",
        )
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
    import os

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

    old_env: dict[str, str | None] = {}
    for arg_key, env_key in env_map.items():
        value = config.args.get(arg_key, "")
        if value:
            old_env[env_key] = os.environ.get(env_key)
            os.environ[env_key] = str(value)

    try:
        result = ACTIONS[action]()
        return json.dumps(result, indent=2)
    finally:
        for env_key in old_env:
            if old_env[env_key] is None:
                os.environ.pop(env_key, None)
            else:
                old_value = old_env[env_key]
                assert old_value is not None
                os.environ[env_key] = old_value


def _ensure_image(image: str) -> str:
    """Build the Docker image if needed, with build deduplication.

    Delegates to :class:`ImageBuildCache` so concurrent and repeated
    calls only trigger a single ``docker build``.
    """
    return _image_cache.ensure_image(image)


def _ensure_image_uncached(image: str) -> str:
    """Build the Docker image if it doesn't already exist (no caching).

    For executor images the tag is derived from a content hash of the
    executor source directory so that code changes automatically trigger
    a rebuild.  Returns the image reference that should be used to run
    the container (may differ from *image* when a hash tag is applied).

    Derives Dockerfile/build context from image name:
      executor-gmail-modify:latest -> -f src/executors/gmail_modify/Dockerfile src/executors/
      llm-runner:latest            -> src/llm/
    """
    base = image.split(":")[0]

    if base.startswith("executor-"):
        name = base.removeprefix("executor-").replace("-", "_")
        executor_dir = Path("src/executors") / name
        content_hash = _compute_executor_hash(executor_dir)
        hashed_image = f"{base}:{content_hash}"

        # Already built with this hash – nothing to do.
        inspect = subprocess.run(
            ["docker", "image", "inspect", hashed_image],
            capture_output=True,
        )
        if inspect.returncode == 0:
            return hashed_image

        context = Path("src/executors")
        dockerfile = executor_dir / "Dockerfile"
        if not dockerfile.exists():
            raise FileNotFoundError(f"No Dockerfile at {dockerfile} for image {image}")

        _build_image(
            tags=[hashed_image, f"{base}:latest"],
            dockerfile=dockerfile,
            context=context,
        )
        return hashed_image

    # Non-executor images: use existing tag-based check.
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    if result.returncode == 0:
        return image

    if base == "llm-runner":
        context = Path("src/llm")
        dockerfile = context / "Dockerfile"
    else:
        context = Path("src") / base.replace("-", "_")
        if not context.exists():
            context = Path("src") / base
        dockerfile = context / "Dockerfile"

    if not dockerfile.exists():
        raise FileNotFoundError(f"No Dockerfile at {dockerfile} for image {image}")

    _build_image(tags=[image], dockerfile=dockerfile, context=context)
    return image


def _build_image(
    tags: list[str],
    dockerfile: Path,
    context: Path,
) -> None:
    """Run ``docker build`` with one or more ``-t`` tags."""
    build_cmd: list[str] = ["docker", "build"]
    for t in tags:
        build_cmd.extend(["-t", t])
    build_cmd.extend(["-f", str(dockerfile), str(context)])

    logger.info(
        "Building image %s from %s (Dockerfile: %s)", tags[0], context, dockerfile
    )
    build_result = subprocess.run(
        build_cmd,
        capture_output=True,
        text=True,
    )
    if build_result.returncode != 0:
        build_err = (
            build_result.stderr.strip() if build_result.stderr else "unknown error"
        )
        logger.error("Docker build failed for %s:\n%s", tags[0], build_err)
        raise RuntimeError(f"Docker build failed for {tags[0]}: {build_err[:500]}")


def collect_required_images(agent_def: "AgentDefinition") -> list[str]:
    """Derive the set of Docker images needed by an agent's tools.

    Uses the same naming convention as :pyattr:`ExecutorConfig.image`:
    ``executor-{name}:latest`` (underscores → hyphens).
    ``ToolConfig.image`` overrides the derived name when set.
    The ``llm-runner:latest`` image is included when tools are present
    (agent mode requires the containerised LLM runner).
    """

    images: set[str] = set()
    for _tool_name, tool_config in agent_def.tools.items():
        if tool_config.image:
            images.add(tool_config.image)
        else:
            image = f"executor-{tool_config.executor.replace('_', '-')}:latest"
            images.add(image)
    if agent_def.tools:
        images.add("llm-runner:latest")
    return sorted(images)


def prebuild_images(agent_def: "AgentDefinition") -> list[threading.Thread]:
    """Kick off background image builds for all tools in the agent definition.

    Returns the list of spawned threads (callers are not expected to join).
    """
    images = collect_required_images(agent_def)
    logger.info("Pre-building %d Docker image(s): %s", len(images), images)
    return _image_cache.start_prebuild(images)


def _run_executor_container(
    config: ExecutorConfig,
    tool_config: "ToolConfig | None" = None,
    bridge_config: BridgeConfig | None = None,
) -> str:
    """Run an executor in an isolated Docker container.

    Captures both stdout (data) and stderr (logs/errors). Stderr is
    always logged at DEBUG on success and ERROR on failure. The
    request_id is passed into the container as ``CREEL_REQUEST_ID``
    for log correlation.

    Args:
        config: Executor configuration
        tool_config: Optional tool configuration with mount/network/image overrides
        bridge_config: Optional bridge configuration for macOS host tools
    """
    from taskrunner.log import request_id_var

    # Determine image to use - tool config overrides executor config
    image = tool_config.image if (tool_config and tool_config.image) else config.image
    image = _ensure_image(image)

    env_vars: dict[str, str] = {}

    # Decrypt and inject secrets
    if config.secrets:
        env_vars.update(decrypt_env_file(config.secrets))

    # Pass args as env vars
    for key, value in config.args.items():
        env_vars[key.upper()] = value

    # Never pass refresh-token JSON into executor containers.
    _replace_google_credentials_with_access_token(env_vars)

    # Pass request ID for correlation
    rid = request_id_var.get(None)
    if rid:
        env_vars["CREEL_REQUEST_ID"] = rid

    # Add bridge configuration if enabled
    if bridge_config and bridge_config.enabled:
        import os

        # Rewrite localhost to host.docker.internal for container access
        bridge_url = bridge_config.url
        bridge_url = bridge_url.replace("://localhost", "://host.docker.internal")
        bridge_url = bridge_url.replace("://127.0.0.1", "://host.docker.internal")
        env_vars["BRIDGE_URL"] = bridge_url
        # Look up scoped token by executor name (e.g. browser → BRIDGE_TOKEN_BROWSER)
        executor_name = config.name or ""
        scope_name = _EXECUTOR_TO_BRIDGE_SCOPE.get(executor_name, executor_name.upper())
        scoped_token = os.environ.get(f"BRIDGE_TOKEN_{scope_name}", "")
        if scoped_token:
            env_vars["BRIDGE_TOKEN"] = scoped_token
        elif bridge_config.token:
            env_vars["BRIDGE_TOKEN"] = bridge_config.token

    # Handle workspace mount for file_ops (must be before env_file write
    # so WORKSPACE=/workspace ends up in the env file, not the host path)
    import os

    _workspace_mount: tuple[str, str] | None = None
    workspace_path = config.args.get("workspace")
    if workspace_path and config.name in ("file_ops",):
        resolved_ws = os.path.realpath(workspace_path)
        if not os.path.isdir(resolved_ws):
            raise RuntimeError("Workspace directory no longer exists")
        # Use read-only mount for read/list operations
        action = config.args.get("action", "")
        mount_mode = "ro" if action in ("read", "list") else "rw"
        _workspace_mount = (resolved_ws, mount_mode)
        env_vars["WORKSPACE"] = "/workspace"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=True, prefix="creel-"
    ) as env_file:
        for key, value in env_vars.items():
            # Sanitize values to prevent env-file newline injection
            sanitized = value.replace("\n", "").replace("\r", "")
            env_file.write(f"{key}={sanitized}\n")
        env_file.flush()

        # Build docker run command
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16M",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=256m",
            "--cpus=0.5",
            "--env-file",
            env_file.name,
        ]

        # Add mount options from tool config
        if tool_config and tool_config.mounts:
            for mount in tool_config.mounts:
                # Expand ~ to home directory
                host_path = os.path.expanduser(mount.path)
                docker_cmd.extend(["-v", f"{host_path}:/mnt{host_path}:{mount.mode}"])

        # Mount dynamic workspace for file_ops executor
        if _workspace_mount:
            docker_cmd.extend(
                ["-v", f"{_workspace_mount[0]}:/workspace:{_workspace_mount[1]}"]
            )

        # Add network isolation if disabled
        if tool_config and not tool_config.network:
            docker_cmd.extend(["--network=none"])

        # Add image name
        docker_cmd.append(image)

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=config.timeout,
            )
        except subprocess.TimeoutExpired as e:
            stderr = (e.stderr or "").strip() if isinstance(e.stderr, str) else ""
            if stderr:
                logger.error(
                    "Executor %s stderr (timeout after %ds):\n%s",
                    config.name,
                    config.timeout,
                    stderr,
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
            logger.error(
                "Executor %s stderr (exit %d):\n%s",
                config.name,
                result.returncode,
                stderr,
            )

    if result.returncode != 0:
        # Include stderr in the error so it propagates to the LLM
        error_detail = stderr[:500] if stderr else f"exit code {result.returncode}"
        raise RuntimeError(f"Executor '{config.name}' failed: {error_detail}")

    return result.stdout.strip()


def _load_secrets_to_env(secrets_path: str) -> None:
    """Decrypt a secrets file and load values into the environment."""
    import os

    secrets = decrypt_env_file(secrets_path)
    for key, value in secrets.items():
        os.environ[key] = value
