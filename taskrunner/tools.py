"""Tool system - bridges YAML tool configs to Anthropic tool definitions and executor execution."""

from __future__ import annotations

import logging

from taskrunner.models import BridgeConfig, ExecutorConfig, ToolConfig
from taskrunner.orchestrator import _run_executor_container, _run_executor_inline

logger = logging.getLogger(__name__)


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
]


def build_tool_definitions(
    tools_config: dict[str, ToolConfig],
    include_memory_tools: bool = False,
) -> list[dict]:
    """Convert YAML tool configs to Anthropic API tool definitions.

    Args:
        tools_config: Mapping of tool name -> ToolConfig.
        include_memory_tools: If True, include built-in memory tools.

    Returns:
        List of Anthropic tool definition dicts ready for the API.
    """
    tool_defs = []

    if include_memory_tools:
        tool_defs.extend(BUILTIN_MEMORY_TOOLS)
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

        tool_defs.append({
            "name": name,
            "description": cfg.description,
            "input_schema": schema,
        })

    return tool_defs


def execute_tool_call(
    tool_name: str,
    tool_input: dict,
    tools_config: dict[str, ToolConfig],
    use_containers: bool = False,
    memory_manager: object | None = None,
    bridge_config: BridgeConfig | None = None,
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

    Returns:
        The executor output as a string.

    Raises:
        ValueError: If tool_name is not found in tools_config.
    """
    # Handle built-in tools
    if tool_name == "remember" and memory_manager is not None:
        from taskrunner.memory import MemoryManager

        text = tool_input.get("text", "")
        category = tool_input.get("category", "general")
        return memory_manager.remember(text, category)

    if tool_name == "update_long_term_memory" and memory_manager is not None:
        from taskrunner.memory import MemoryManager

        text = tool_input.get("text", "")
        return memory_manager.update_long_term(text)

    if tool_name not in tools_config:
        raise ValueError(f"Unknown tool: {tool_name}")

    cfg = tools_config[tool_name]

    # Merge: LLM input as base, fixed_args override
    merged_args = {**tool_input, **cfg.fixed_args}

    # Convert all values to strings (executors expect string args)
    string_args = {k: str(v) for k, v in merged_args.items()}

    # Build a ExecutorConfig for the existing infrastructure
    executor_config = ExecutorConfig(
        name=cfg.executor,
        secrets=cfg.secrets,
        args=string_args,
    )

    logger.info("Executing tool %s (executor: %s)", tool_name, cfg.executor)

    if use_containers:
        return _run_executor_container(executor_config, cfg, bridge_config)
    return _run_executor_inline(cfg.executor, executor_config)
