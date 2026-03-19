"""Orchestrator - the core task execution loop.

Reads task definitions, runs executors, calls LLM, routes output.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Generator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

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

logger = logging.getLogger(__name__)

_GOOGLE_TOKEN_MAX_AGE_SECONDS = 3600

# Lock for inline executor env-var mutations (os.environ is process-global)
_ENV_LOCK = threading.RLock()

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

    from creel.skills.registry import get_shared_registry

    _registry = get_shared_registry()

    if use_containers:
        from creel.container_agent import run_agent_loop_container

        agent_result = run_agent_loop_container(
            messages=messages,
            llm_config=task.llm,
            agent_config=task.agent,
            registry=_registry,
            skill_overrides=task.skills if hasattr(task, "skills") else {},
            use_containers=use_containers,
        )
    else:
        from creel.agent import run_agent_loop

        agent_result = run_agent_loop(
            messages=messages,
            llm_config=task.llm,
            agent_config=task.agent,
            registry=_registry,
            skill_overrides=task.skills if hasattr(task, "skills") else {},
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
    """Run an executor by looking up its skill registration (no container).

    Uses _env_override to temporarily set secrets and bridge env vars.
    """
    from creel.skills.registry import get_shared_registry

    # Load secrets if configured
    env_overrides: dict[str, str] = {}
    if config.secrets:
        env_overrides = decrypt_env_file(config.secrets)
        _replace_google_credentials_with_access_token(env_overrides)

    registry = get_shared_registry()

    entry = registry.get_skill(name)
    if entry is None:
        raise ValueError(f"Unknown inline executor: {name}")

    meta = entry.meta
    # Inject BRIDGE_URL and scoped BRIDGE_TOKEN for bridge-calling executors
    if meta.needs_bridge or meta.bridge_scope:
        if "BRIDGE_URL" not in env_overrides and not os.environ.get("BRIDGE_URL"):
            env_overrides["BRIDGE_URL"] = os.environ.get(
                "CREEL_BRIDGE_URL", "http://localhost:8099"
            )
        scope = meta.bridge_scope or meta.id.upper()
        if "BRIDGE_TOKEN" not in env_overrides and not os.environ.get("BRIDGE_TOKEN"):
            scoped_token = os.environ.get(f"BRIDGE_TOKEN_{scope}", "")
            if scoped_token:
                env_overrides["BRIDGE_TOKEN"] = scoped_token

    with _env_override(env_overrides):
        return entry.execute(config)


def _load_secrets_to_env(secrets_path: str) -> None:
    """Decrypt a secrets file and load values into the environment."""
    secrets = decrypt_env_file(secrets_path)
    for key, value in secrets.items():
        os.environ[key] = value
