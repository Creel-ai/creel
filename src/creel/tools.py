"""Tool system - bridges YAML tool configs to Anthropic tool definitions and executor execution."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from creel.containers import _run_executor_container
from creel.models import BridgeConfig, ExecutorConfig, SessionState, ToolConfig
from creel.orchestrator import _run_executor_inline

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
    tools_config: dict[str, ToolConfig],
    include_memory_tools: bool = False,
    include_workspace_tools: bool = False,
    include_cron_tools: bool = False,
    include_subagent_tool: bool = False,
) -> list[dict]:
    """Convert YAML tool configs to Anthropic API tool definitions.

    Args:
        tools_config: Mapping of tool name -> ToolConfig.
        include_memory_tools: If True, include built-in memory tools.
        include_workspace_tools: If True, include built-in workspace tools
            (set_workspace).
        include_cron_tools: If True, include built-in cron scheduling tool.
        include_subagent_tool: If True, include built-in sub-agent tool.

    Returns:
        List of Anthropic tool definition dicts ready for the API.
    """
    tool_defs = []

    if include_memory_tools:
        tool_defs.extend(BUILTIN_MEMORY_TOOLS)
    if include_workspace_tools:
        tool_defs.extend(BUILTIN_WORKSPACE_TOOLS)
    if include_cron_tools:
        from creel.cron.tool import CRON_TOOL_DEFINITION

        tool_defs.append(CRON_TOOL_DEFINITION)
    if include_subagent_tool:
        tool_defs.append(BUILTIN_SUBAGENT_TOOL)
    for name, cfg in tools_config.items():
        properties: dict[str, dict] = {}
        required: list[str] = []

        for param_name, param in cfg.parameters.items():
            # Skip parameters that are overridden by fixed_args
            if param_name in cfg.fixed_args:
                continue
            properties[param_name] = {
                "type": param.type,
                "description": param.description,
            }
            if param.required:
                required.append(param_name)

        schema: dict = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required

        tool_defs.append(
            {
                "name": name,
                "description": cfg.description,
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
    tools_config: dict[str, ToolConfig],
    use_containers: bool = False,
    memory_manager: Any | None = None,
    bridge_config: BridgeConfig | None = None,
    session_state: SessionState | None = None,
    cron_manager: Any | None = None,
    subagent_manager: Any | None = None,
    container_pool: ContainerPool | None = None,
) -> str:
    """Execute a tool call via the corresponding executor.

    Merges fixed_args over LLM-provided input (fixed_args always win).
    Converts the tool config into a ExecutorConfig and delegates to
    the existing executor infrastructure.

    Args:
        tool_name: Name of the tool to execute.
        tool_input: Arguments provided by the LLM.
        tools_config: All tool configurations.
        use_containers: If True, run in Docker container.
        memory_manager: Optional memory manager for memory tools.
        bridge_config: Optional bridge configuration.
        session_state: Optional per-session state. Used to store/read
            workspace path for file_ops tools.
        cron_manager: Optional CronManager for cron tool.
        subagent_manager: Optional SubAgentManager for sub-agent tool.
        container_pool: Optional ContainerPool for persistent coding containers.

    Returns:
        The executor output as a string.

    Raises:
        ValueError: If tool_name is not found in tools_config.
    """
    # Handle set_workspace built-in
    if tool_name == "set_workspace":
        # set_workspace requires an active session (not task mode)
        if session_state is None:
            return json.dumps({"error": "set_workspace is only available in interactive sessions"})
        path = tool_input.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        error = _validate_workspace_path(path)
        if error:
            return json.dumps({"error": error})
        resolved = os.path.realpath(path)
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

        sender_id = session_state.sender_id if session_state else ""
        return handle_subagent_tool(tool_input, manager=subagent_manager, sender_id=sender_id)

    if tool_name not in tools_config:
        raise ValueError(f"Unknown tool: {tool_name}")

    cfg = tools_config[tool_name]

    # Security: strip 'workspace' from LLM-provided input for file_ops tools
    # to prevent bypassing the set_workspace approval flow.
    if cfg.executor == "file_ops":
        tool_input = {k: v for k, v in tool_input.items() if k != "workspace"}

    # Merge: LLM input as base, fixed_args override
    merged_args = {**tool_input, **cfg.fixed_args}

    # Inject workspace from session_state for file_ops tools
    if cfg.executor == "file_ops" and session_state and session_state.workspace:
        workspace = session_state.workspace
        # Re-validate: workspace may have been removed since set_workspace
        if not os.path.isdir(workspace):
            return json.dumps(
                {"error": "Workspace is no longer valid (directory removed or inaccessible)"}
            )
        merged_args["workspace"] = workspace

    # Convert all values to strings (executors expect string args)
    string_args = {k: str(v) for k, v in merged_args.items()}

    # Build a ExecutorConfig for the existing infrastructure
    executor_config = ExecutorConfig(
        name=cfg.executor,
        secrets=cfg.secrets,
        args=string_args,
        timeout=cfg.timeout,
    )

    logger.info("Executing tool %s (executor: %s)", tool_name, cfg.executor)

    if use_containers:
        # Route coding executor through warm container pool when available
        if cfg.executor == "coding" and container_pool is not None and container_pool.enabled:
            return _run_coding_via_pool(container_pool, executor_config, cfg)
        return _run_executor_container(executor_config, cfg, bridge_config)
    return _run_executor_inline(cfg.executor, executor_config)


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
        timeout = executor_config.timeout

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
