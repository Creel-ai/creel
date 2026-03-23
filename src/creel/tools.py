"""Tool system - bridges YAML tool configs to Anthropic tool definitions and executor execution."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from creel.containers import _run_executor_container
from creel.models import (
    BridgeConfig,
    ExecutorConfig,
    HttpConfig,
    SessionState,
    SkillOverride,
    ToolConfig,
)
from creel.skills.registry import SkillRegistry

if TYPE_CHECKING:
    from creel.container_pool import ContainerPool

logger = logging.getLogger(__name__)

# System directories that must never be used as workspaces.
# Checked via both exact match and prefix match (e.g. /etc/nginx is also blocked).
_SYSTEM_DIRS = frozenset(
    {
        "/etc",
        "/var",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/boot",
        "/dev",
        "/proc",
        "/sys",
    }
)

# User-sensitive directories (relative to home) that should be blocked.
_SENSITIVE_HOME_DIRS = (".ssh", ".gnupg", ".age", ".aws")


BUILTIN_MEMORY_TOOLS = [
    {
        "name": "remember",
        "description": (
            "Save important information to daily memory. Use this when the user "
            "asks you to remember something, or when you encounter information "
            "worth preserving across sessions (decisions, preferences, facts)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The information to remember.",
                },
                "category": {
                    "type": "string",
                    "description": "Category tag (e.g. 'preference', 'decision', 'fact', 'general').",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "update_long_term_memory",
        "description": (
            "Update curated long-term memory (MEMORY.md) with distilled, "
            "important information that should persist indefinitely. Use sparingly "
            "for significant facts, not daily notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Content to append to long-term memory.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Search across all memory files (daily logs and long-term) for entries "
            "matching a query. Returns results with date and line references you can "
            "use with edit_memory or delete_memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term (case-insensitive substring match).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 20).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "delete_memory",
        "description": (
            "Delete a specific memory entry by date and line number. "
            "Use search_memory first to find the entry reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date of the memory file (YYYY-MM-DD) or 'long_term' for MEMORY.md.",
                },
                "line_number": {
                    "type": "integer",
                    "description": "1-based line number within the file.",
                },
            },
            "required": ["date", "line_number"],
        },
    },
    {
        "name": "edit_memory",
        "description": (
            "Edit a specific memory entry by date and line number. "
            "Use search_memory first to find the entry reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date of the memory file (YYYY-MM-DD) or 'long_term' for MEMORY.md.",
                },
                "line_number": {
                    "type": "integer",
                    "description": "1-based line number within the file.",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text for the line.",
                },
            },
            "required": ["date", "line_number", "new_text"],
        },
    },
    {
        "name": "list_memory_files",
        "description": (
            "List all memory files with entry counts and sizes. "
            "Shows daily files (newest first) and long-term memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


BUILTIN_SUBAGENT_TOOL = {
    "name": "subagent",
    "description": (
        "Spawn a background sub-agent for parallel or long-running tasks. "
        "Sub-agents run independently with their own conversation and can use "
        "the same tools as you. Use action='spawn' to start one, 'list' to "
        "check status, 'steer' to send follow-up instructions, or 'kill' to stop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["spawn", "list", "steer", "kill"],
                "description": "Action to perform.",
            },
            "task": {
                "type": "string",
                "description": "Task description for the sub-agent (required for spawn).",
            },
            "label": {
                "type": "string",
                "description": "Human-readable label for this sub-agent.",
            },
            "model": {
                "type": "string",
                "description": "Override model for this sub-agent (default: same as parent).",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 300).",
            },
            "agent_id": {
                "type": "string",
                "description": "Sub-agent ID (required for steer/kill).",
            },
            "message": {
                "type": "string",
                "description": "Follow-up message to inject (required for steer).",
            },
        },
        "required": ["action"],
    },
}


BUILTIN_KB_TOOLS = [
    {
        "name": "kb_search",
        "description": (
            "Search the knowledge base for relevant documents. The knowledge base "
            "contains indexed personal documents (notes, docs, code files). Returns "
            "matching text chunks with source file attribution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — use natural language or keywords.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 5).",
                },
                "filter": {
                    "type": "string",
                    "description": "Optional path prefix to filter results by source file.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_add",
        "description": (
            "Add a file or directory to the knowledge base for indexing. "
            "Supports markdown, text, PDF, and code files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to a file or directory to index.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "kb_list",
        "description": "List all documents currently indexed in the knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "kb_stats",
        "description": "Get knowledge base statistics (document count, chunk count, size).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


BUILTIN_WORKSPACE_TOOLS = [
    {
        "name": "set_workspace",
        "description": (
            "Set the workspace directory for file operations. Call this before "
            "using read_file, write_file, edit_file, or list_files to operate "
            "on a specific directory. The path must be an absolute directory "
            "that exists on the host. Requires user approval on first use."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the directory to use as workspace.",
                },
            },
            "required": ["path"],
        },
    },
]


def build_tool_definitions(
    registry: SkillRegistry,
    skill_overrides: dict[str, SkillOverride],
    include_memory_tools: bool = False,
    include_workspace_tools: bool = False,
    include_cron_tools: bool = False,
    include_subagent_tool: bool = False,
    include_kb_tools: bool = False,
) -> list[dict]:
    """Convert skill registry metadata to Anthropic API tool definitions.

    Iterates enabled skills in ``skill_overrides``, looks up each in the
    registry, and builds tool definitions from the intrinsic ``ToolSpec``
    metadata.

    Returns:
        List of Anthropic tool definition dicts ready for the API.
    """
    tool_defs: list[dict] = []

    if include_memory_tools:
        tool_defs.extend(BUILTIN_MEMORY_TOOLS)
    if include_workspace_tools:
        tool_defs.extend(BUILTIN_WORKSPACE_TOOLS)
    if include_cron_tools:
        from creel.cron.tool import CRON_TOOL_DEFINITION

        tool_defs.append(CRON_TOOL_DEFINITION)
    if include_subagent_tool:
        tool_defs.append(BUILTIN_SUBAGENT_TOOL)
    if include_kb_tools:
        tool_defs.extend(BUILTIN_KB_TOOLS)

    for skill_id, override in skill_overrides.items():
        if not override.enabled:
            continue
        entry = registry.get_skill(skill_id)
        if entry is None:
            logger.warning("Skill '%s' in config but not registered — skipping", skill_id)
            continue
        for tool_spec in entry.meta.tools:
            properties: dict[str, dict] = {}
            required: list[str] = []
            for param in tool_spec.params:
                if param.name in tool_spec.fixed_args:
                    continue
                properties[param.name] = {
                    "type": param.type,
                    "description": param.description,
                }
                if param.required:
                    required.append(param.name)

            schema: dict = {
                "type": "object",
                "properties": properties,
            }
            if required:
                schema["required"] = required

            tool_defs.append(
                {
                    "name": tool_spec.name,
                    "description": tool_spec.description,
                    "input_schema": schema,
                }
            )

    return tool_defs


def _is_blocked_path(path: str) -> bool:
    """Check if a resolved path is a blocked system or sensitive location."""
    if path == "/":
        return True
    # Prefix match against system directories (blocks /etc AND /etc/nginx/...)
    for d in _SYSTEM_DIRS:
        if path == d or path.startswith(d + "/"):
            return True
    # Block sensitive user directories
    home = os.path.expanduser("~")
    if home != "~":
        for name in _SENSITIVE_HOME_DIRS:
            sensitive = os.path.join(home, name)
            if path == sensitive or path.startswith(sensitive + "/"):
                return True
    return False


def _validate_workspace_path(path: str) -> str | None:
    """Validate a workspace path and return an error message or None if valid.

    Returns a generic error message to avoid leaking path details to the LLM.
    """
    if not os.path.isabs(path):
        return "Path must be absolute"
    resolved = os.path.realpath(path)
    # Check both the literal path and the resolved path (handles symlinks)
    if _is_blocked_path(path) or _is_blocked_path(resolved):
        return "Cannot use this path as workspace"
    if not os.path.exists(resolved):
        return "Path does not exist"
    if not os.path.isdir(resolved):
        return "Path is not a directory"
    return None


def execute_tool_call(
    tool_name: str,
    tool_input: dict,
    registry: SkillRegistry,
    skill_overrides: dict[str, SkillOverride],
    use_containers: bool = False,
    memory_manager: Any | None = None,
    bridge_config: BridgeConfig | None = None,
    session_state: SessionState | None = None,
    cron_manager: Any | None = None,
    subagent_manager: Any | None = None,
    container_pool: ContainerPool | None = None,
    kb_manager: Any | None = None,
) -> str:
    """Execute a tool call via the skill registry.

    Looks up the tool in the registry, merges fixed_args over LLM-provided
    input, and delegates to the skill's execute function (inline) or to
    the container infrastructure.

    Returns:
        The executor output as a string.

    Raises:
        ValueError: If tool_name is not found in the registry.
    """
    # Handle set_workspace built-in
    if tool_name == "set_workspace":
        if session_state is None:
            return json.dumps({"error": "set_workspace is only available in interactive sessions"})
        path = tool_input.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        error = _validate_workspace_path(path)
        if error:
            return json.dumps({"error": error})
        resolved = os.path.realpath(path)
        if isinstance(session_state, dict):
            session_state["workspace"] = resolved
        else:
            session_state.workspace = resolved
        return json.dumps({"workspace": resolved, "status": "ok"})

    # Handle built-in tools
    if tool_name == "remember" and memory_manager is not None:
        text = tool_input.get("text", "")
        category = tool_input.get("category", "general")
        return memory_manager.remember(text, category)

    if tool_name == "update_long_term_memory" and memory_manager is not None:
        text = tool_input.get("text", "")
        return memory_manager.update_long_term(text)

    if tool_name == "search_memory" and memory_manager is not None:
        query = tool_input.get("query", "")
        max_results = tool_input.get("max_results", 20)
        return memory_manager.search_memory(query, max_results=int(max_results))

    if tool_name == "delete_memory" and memory_manager is not None:
        date_str = tool_input.get("date", "")
        line_number = int(tool_input.get("line_number", 0))
        return memory_manager.delete_memory(date_str, line_number)

    if tool_name == "edit_memory" and memory_manager is not None:
        date_str = tool_input.get("date", "")
        line_number = int(tool_input.get("line_number", 0))
        new_text = tool_input.get("new_text", "")
        return memory_manager.edit_memory(date_str, line_number, new_text)

    if tool_name == "list_memory_files" and memory_manager is not None:
        return memory_manager.list_memory_files()

    if tool_name == "cron" and cron_manager is not None:
        from creel.cron.tool import handle_cron_tool

        return handle_cron_tool(tool_input, cron_manager)

    if tool_name == "subagent" and subagent_manager is not None:
        from creel.subagents.executor import handle_subagent_tool

        sender_id = (
            (
                session_state.get("sender_id", "")
                if isinstance(session_state, dict)
                else getattr(session_state, "sender_id", "")
            )
            if session_state
            else ""
        )
        return handle_subagent_tool(tool_input, manager=subagent_manager, sender_id=sender_id)

    # Handle knowledge base built-in tools
    if tool_name in ("kb_search", "kb_add", "kb_list", "kb_stats") and kb_manager is not None:
        return _handle_kb_tool(tool_name, tool_input, kb_manager)

    # --- Skill-based execution ---
    skill_result = registry.get_tool(tool_name)
    if skill_result is None:
        raise ValueError(f"Unknown tool: {tool_name}")

    # Security: verify the skill is enabled in skill_overrides before executing
    _, entry = skill_result
    skill_id = entry.meta.id
    if skill_id not in skill_overrides:
        raise ValueError(
            f"Tool '{tool_name}' (skill '{skill_id}') is not enabled in skill_overrides"
        )
    override = skill_overrides[skill_id]
    if not override.enabled:
        raise ValueError(f"Tool '{tool_name}' (skill '{skill_id}') is disabled")

    return _execute_skill_tool(
        tool_name=tool_name,
        tool_input=tool_input,
        skill_result=skill_result,
        skill_overrides=skill_overrides,
        use_containers=use_containers,
        bridge_config=bridge_config,
        session_state=session_state,
        container_pool=container_pool,
    )


def _execute_skill_tool(
    tool_name: str,
    tool_input: dict,
    skill_result: tuple,
    skill_overrides: dict[str, SkillOverride],
    use_containers: bool,
    bridge_config: BridgeConfig | None,
    session_state: SessionState | None,
    container_pool: Any | None,
) -> str:
    """Execute a tool via the skill registry.

    Merges fixed_args, builds an ExecutorConfig, and delegates to
    _run_executor_inline_skill() which handles secrets/bridge injection
    and calls the skill's execute function.
    """

    tool_spec, entry = skill_result
    skill_id = entry.meta.id
    override = skill_overrides.get(skill_id, SkillOverride())

    # Security: strip 'workspace' from LLM-provided input for file_ops
    if skill_id == "file_ops":
        tool_input = {k: v for k, v in tool_input.items() if k != "workspace"}

    # Merge: LLM input as base, fixed_args override
    merged_args = {**tool_input, **tool_spec.fixed_args}

    # Inject workspace from session_state for file_ops tools
    _ws = (
        (
            session_state.get("workspace")
            if isinstance(session_state, dict)
            else getattr(session_state, "workspace", None)
        )
        if session_state
        else None
    )
    if skill_id == "file_ops" and _ws:
        workspace = _ws
        if not os.path.isdir(workspace):
            return json.dumps(
                {"error": "Workspace is no longer valid (directory removed or inaccessible)"}
            )
        merged_args["workspace"] = workspace

    # Convert all values to strings (executors expect string args)
    string_args = {k: str(v) for k, v in merged_args.items()}

    # Resolve secrets: per-tool override wins over skill-level override
    secrets = override.secrets
    if override.tools:
        tool_override = override.tools.get(tool_name)
        if tool_override and tool_override.secrets:
            secrets = tool_override.secrets

    # Build ExecutorConfig from skill override
    executor_config = ExecutorConfig(
        name=skill_id,
        secrets=secrets,
        args=string_args,
        timeout=override.timeout or 60,
        http=override.http or HttpConfig(),
    )

    logger.info("Executing skill tool %s (skill: %s)", tool_name, skill_id)

    if use_containers:
        # Build a ToolConfig from skill metadata + override for container execution
        tool_config = _build_tool_config_from_skill(entry.meta, override, tool_spec)
        if skill_id == "coding" and container_pool is not None and container_pool.enabled:
            return _run_coding_via_pool(container_pool, executor_config, tool_config)
        if skill_id == "exec_interactive":
            return _run_interactive_via_container(executor_config, tool_config)
        if skill_id == "dev_session":
            return _run_dev_session(executor_config, tool_config)
        return _run_executor_container(executor_config, tool_config, bridge_config)

    return _run_executor_inline_skill(entry, tool_name, executor_config)


def _run_executor_inline_skill(entry: Any, tool_name: str, config: ExecutorConfig) -> str:
    """Run a skill executor inline: decrypt secrets, inject bridge env, call execute."""
    from creel.orchestrator import _env_override, _replace_google_credentials_with_access_token
    from creel.secrets import decrypt_env_file

    env_overrides: dict[str, str] = {}
    if config.secrets:
        env_overrides = decrypt_env_file(config.secrets)
        _replace_google_credentials_with_access_token(env_overrides)

    meta = entry.meta
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


def _build_tool_config_from_skill(meta: Any, override: SkillOverride, tool_spec: Any) -> ToolConfig:
    """Synthesize a ToolConfig from SkillMeta + SkillOverride for container execution."""
    return ToolConfig(
        executor=meta.id,
        description=tool_spec.description,
        secrets=override.secrets,
        network=override.network if override.network is not None else meta.needs_network,
        writable=override.writable or False,
        memory=override.memory or "256m",
        cpus=override.cpus or "0.5",
        tmpfs_size=override.tmpfs_size or "16M",
        timeout=override.timeout or 60,
        image=override.image,
        dockerfile=override.dockerfile,
        host_auth=override.host_auth or False,
        mounts=override.mounts or [],
        classify_output=override.classify_output or False,
        cache_ttl=override.cache_ttl or 0,
        http=override.http or HttpConfig(),
    )


_KB_BLOCKED_PATHS = frozenset(
    {
        ".ssh",
        ".age",
        ".gnupg",
        ".aws",
        ".kube",
        ".docker",
        ".config/gh",
        ".config/gcloud",
        ".env",
        "secrets",
        ".local/share/keyrings",
    }
)

_KB_BLOCKED_SYSTEM_DIRS = frozenset({"/etc", "/var/run", "/var/tmp", "/proc", "/sys", "/tmp"})


def _is_kb_path_safe(path_str: str) -> bool:
    """Check that a path doesn't point to sensitive directories."""
    resolved = os.path.realpath(os.path.expanduser(path_str))
    home = os.path.expanduser("~")
    for blocked in _KB_BLOCKED_PATHS:
        blocked_path = os.path.join(home, blocked)
        if resolved == blocked_path or resolved.startswith(blocked_path + os.sep):
            return False
    for system_dir in _KB_BLOCKED_SYSTEM_DIRS:
        if resolved == system_dir or resolved.startswith(system_dir + os.sep):
            return False
    return True


def _redact_source_path(source: str) -> str:
    """Replace absolute home prefix with ~ for LLM-facing output."""
    home = os.path.expanduser("~")
    if source.startswith(home):
        return "~" + source[len(home) :]
    return source


def _handle_kb_tool(tool_name: str, tool_input: dict, kb_manager: Any) -> str:
    """Handle knowledge base built-in tool calls."""
    if tool_name == "kb_search":
        query = tool_input.get("query", "")
        try:
            top_k = int(tool_input.get("top_k", 5))
        except (ValueError, TypeError):
            top_k = 5
        filter_path = tool_input.get("filter") or None
        results = kb_manager.search(query, top_k=top_k, filter_path=filter_path)
        if not results:
            return json.dumps({"results": [], "message": f"No results found for '{query}'."})
        # Redact absolute paths before returning to LLM
        for r in results:
            r["source"] = _redact_source_path(r["source"])
        return json.dumps({"results": results, "count": len(results)}, indent=2)

    if tool_name == "kb_add":
        path = tool_input.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        if not _is_kb_path_safe(path):
            return json.dumps({"error": f"Refused to index sensitive path: {path}"})
        result = kb_manager.add(path)
        summary = {k: v for k, v in result.items() if k != "files"}
        summary["files_indexed"] = result.get("files", [])
        return json.dumps(summary, indent=2)

    if tool_name == "kb_list":
        docs = kb_manager.list_documents()
        if not docs:
            return json.dumps({"documents": [], "message": "Knowledge base is empty."})
        # Redact paths
        for d in docs:
            d["path"] = _redact_source_path(d["path"])
        return json.dumps({"documents": docs, "count": len(docs)}, indent=2)

    if tool_name == "kb_stats":
        return json.dumps(kb_manager.stats(), indent=2)

    return json.dumps({"error": f"Unknown KB tool: {tool_name}"})


def _run_coding_via_pool(
    pool: ContainerPool,
    executor_config: ExecutorConfig,
    tool_config: ToolConfig,
) -> str:
    """Execute a coding command using a persistent pooled container.

    Acquires a warm container running dev_runner.py, sends the execute
    command, and releases the container back to the pool for reuse.
    """
    from creel.containers import _ensure_image

    image = tool_config.image if tool_config.image else executor_config.image
    image = _ensure_image(image)

    # Docker flags matching the tool config overrides
    docker_flags = []
    if not tool_config.writable:
        docker_flags.append("--read-only")
    docker_flags.extend(
        [
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={tool_config.tmpfs_size}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--memory={tool_config.memory}",
            f"--cpus={tool_config.cpus}",
        ]
    )
    if not tool_config.network:
        docker_flags.append("--network=none")

    container = pool.acquire(
        image=image,
        entrypoint="python /app/dev_runner.py",
        docker_flags=docker_flags,
        env_vars={},
    )

    try:
        command = executor_config.args.get("COMMAND", executor_config.args.get("command", ""))
        workdir = executor_config.args.get("WORKDIR", executor_config.args.get("workdir"))
        timeout = executor_config.timeout or 60

        container.send(
            {
                "type": "execute",
                "command": command,
                "workdir": workdir,
                "timeout": timeout,
            }
        )

        msg = container.recv(timeout=float(timeout + 10))
        pool.release(container)

        if msg.get("type") == "error":
            raise RuntimeError(msg.get("message", "Unknown error from dev container"))

        if msg.get("type") == "result":
            import json as _json

            return _json.dumps(msg, indent=2)

        raise RuntimeError(f"Unexpected message type from dev container: {msg.get('type')}")

    except Exception:
        pool.discard(container)
        raise


def _run_interactive_via_container(
    executor_config: ExecutorConfig,
    tool_config: ToolConfig,
) -> str:
    """Execute an interactive PTY action via a per-session Docker container.

    Each ``start`` action spins up a new container; subsequent actions
    route to the container by session_id; ``close`` tears it down.
    """
    from creel.interactive_sessions import get_session_manager

    return get_session_manager().execute(executor_config, tool_config)


def _run_dev_session(
    executor_config: ExecutorConfig,
    tool_config: ToolConfig,
) -> str:
    """Execute a dev session action via a long-lived Docker container.

    The container runs ``dev_session_runner.py`` with an in-container
    ProcessManager that manages multiple concurrent processes.
    """
    from creel.dev_session_manager import get_dev_session_manager

    return get_dev_session_manager().execute(executor_config, tool_config)
