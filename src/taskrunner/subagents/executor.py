"""Sub-agent tool executor — dispatches tool calls to SubAgentManager."""

from __future__ import annotations

import json
import logging
from typing import Any

from taskrunner.subagents.manager import SubAgentManager
from taskrunner.subagents.models import SubAgentConfig

logger = logging.getLogger(__name__)


def handle_subagent_tool(tool_input: dict[str, Any], manager: SubAgentManager) -> str:
    """Handle a sub-agent tool call by dispatching on ``action``.

    Returns a JSON string suitable for a tool_result.
    """
    action = tool_input.get("action", "")

    if action == "spawn":
        return _handle_spawn(tool_input, manager)
    elif action == "list":
        return _handle_list(manager)
    elif action == "steer":
        return _handle_steer(tool_input, manager)
    elif action == "kill":
        return _handle_kill(tool_input, manager)
    else:
        return json.dumps({"error": f"Unknown action: {action!r}. Use spawn, list, steer, or kill."})


def _handle_spawn(tool_input: dict[str, Any], manager: SubAgentManager) -> str:
    task = tool_input.get("task", "")
    if not task:
        return json.dumps({"error": "task is required for spawn"})

    config = SubAgentConfig(
        task=task,
        label=tool_input.get("label", ""),
        model=tool_input.get("model") or None,
        timeout_seconds=int(tool_input.get("timeout", 300)),
    )
    agent_id = manager.spawn(config)
    return json.dumps({
        "agent_id": agent_id,
        "label": config.label or f"subagent-{agent_id}",
        "status": "running",
        "message": f"Sub-agent spawned. Use action='list' to check status.",
    })


def _handle_list(manager: SubAgentManager) -> str:
    agents = manager.list_agents()
    if not agents:
        return json.dumps({"agents": [], "message": "No sub-agents."})

    items = []
    for a in agents:
        item = {
            "id": a.id,
            "label": a.label,
            "status": a.status.value,
            "started_at": a.started_at.isoformat(),
        }
        if a.completed_at:
            item["completed_at"] = a.completed_at.isoformat()
        if a.result_summary:
            summary = a.result_summary
            if len(summary) > 500:
                summary = summary[:500] + "..."
            item["result_summary"] = summary
        if a.error:
            item["error"] = a.error
        items.append(item)

    return json.dumps({"agents": items}, indent=2)


def _handle_steer(tool_input: dict[str, Any], manager: SubAgentManager) -> str:
    agent_id = tool_input.get("agent_id", "")
    message = tool_input.get("message", "")
    if not agent_id:
        return json.dumps({"error": "agent_id is required for steer"})
    if not message:
        return json.dumps({"error": "message is required for steer"})

    ok = manager.steer(agent_id, message)
    if ok:
        return json.dumps({"status": "queued", "agent_id": agent_id})
    return json.dumps({"error": f"Sub-agent {agent_id!r} is not running."})


def _handle_kill(tool_input: dict[str, Any], manager: SubAgentManager) -> str:
    agent_id = tool_input.get("agent_id", "")
    if not agent_id:
        return json.dumps({"error": "agent_id is required for kill"})

    ok = manager.kill(agent_id)
    if ok:
        return json.dumps({"status": "killed", "agent_id": agent_id})
    return json.dumps({"error": f"Sub-agent {agent_id!r} is not running."})
