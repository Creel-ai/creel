"""Tool system - bridges YAML tool configs to Anthropic tool definitions and fetcher execution."""

from __future__ import annotations

import logging

from taskrunner.models import FetcherConfig, ToolConfig
from taskrunner.orchestrator import _run_fetcher_container, _run_fetcher_inline

logger = logging.getLogger(__name__)


def build_tool_definitions(tools_config: dict[str, ToolConfig]) -> list[dict]:
    """Convert YAML tool configs to Anthropic API tool definitions.

    Args:
        tools_config: Mapping of tool name -> ToolConfig.

    Returns:
        List of Anthropic tool definition dicts ready for the API.
    """
    tool_defs = []
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
) -> str:
    """Execute a tool call via the corresponding fetcher.

    Merges fixed_args over LLM-provided input (fixed_args always win).
    Converts the tool config into a FetcherConfig and delegates to
    the existing fetcher infrastructure.

    Args:
        tool_name: Name of the tool to execute.
        tool_input: Arguments provided by the LLM.
        tools_config: All tool configurations.
        use_containers: If True, run in Docker container.

    Returns:
        The fetcher output as a string.

    Raises:
        ValueError: If tool_name is not found in tools_config.
    """
    if tool_name not in tools_config:
        raise ValueError(f"Unknown tool: {tool_name}")

    cfg = tools_config[tool_name]

    # Merge: LLM input as base, fixed_args override
    merged_args = {**tool_input, **cfg.fixed_args}

    # Convert all values to strings (fetchers expect string args)
    string_args = {k: str(v) for k, v in merged_args.items()}

    # Build a FetcherConfig for the existing infrastructure
    fetcher_config = FetcherConfig(
        name=cfg.fetcher,
        secrets=cfg.secrets,
        args=string_args,
    )

    logger.info("Executing tool %s (fetcher: %s)", tool_name, cfg.fetcher)

    if use_containers:
        return _run_fetcher_container(fetcher_config)
    return _run_fetcher_inline(cfg.fetcher, fetcher_config)
