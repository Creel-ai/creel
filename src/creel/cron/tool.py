"""Agent tool for cron job management.

Exposes a single 'cron' tool with actions: list, add, update, remove, run, runs.
The agent can schedule its own reminders and background tasks conversationally.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from creel.cron.manager import CronManager
from creel.cron.models import (
    CronJob,
    Delivery,
    Payload,
    RunRecord,
    Schedule,
)

logger = logging.getLogger(__name__)

CRON_TOOL_DEFINITION = {
    "name": "cron",
    "description": (
        "Manage scheduled cron jobs. You can list, create, update, delete, "
        "trigger, and view run history for jobs. Use this to schedule "
        "reminders, background tasks, and recurring agent actions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "update", "remove", "run", "runs"],
                "description": "The action to perform.",
            },
            "job_id": {
                "type": "string",
                "description": ("Job ID (required for update, remove, run, runs)."),
            },
            "name": {
                "type": "string",
                "description": "Job name (required for add, optional for update).",
            },
            "schedule_kind": {
                "type": "string",
                "enum": ["cron", "every", "at"],
                "description": (
                    "Schedule type (required for add, optional for update). "
                    "'cron' for cron expressions, 'every' for intervals in "
                    "seconds, 'at' for one-shot timestamps."
                ),
            },
            "schedule_expr": {
                "type": "string",
                "description": (
                    "Schedule expression (required for add, optional for update). "
                    "Cron: '0 8 * * *', every: '300', at: '2026-03-01T09:00:00'."
                ),
            },
            "tz": {
                "type": "string",
                "description": "Timezone for the schedule (default: UTC).",
            },
            "message": {
                "type": "string",
                "description": ("The message or prompt for the job payload (required for add)."),
            },
            "target": {
                "type": "string",
                "enum": ["main", "isolated"],
                "description": (
                    "Execution mode: 'main' injects into the current session, "
                    "'isolated' runs a fresh agent turn (default: isolated)."
                ),
            },
            "payload_kind": {
                "type": "string",
                "enum": ["agentTurn", "systemEvent"],
                "description": (
                    "Payload type: 'agentTurn' for a full agent loop, "
                    "'systemEvent' for injecting a message (default: agentTurn). "
                    "systemEvent forces target to 'main'."
                ),
            },
            "model": {
                "type": "string",
                "description": "Model override for isolated agent turns.",
            },
            "delivery_mode": {
                "type": "string",
                "enum": ["announce", "webhook", "none"],
                "description": "How to deliver output from isolated jobs (default: none).",
            },
            "delivery_channel": {
                "type": "string",
                "description": "Channel name for 'announce' delivery mode.",
            },
            "delivery_url": {
                "type": "string",
                "description": "URL for 'webhook' delivery mode.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Timeout in seconds for the job payload (default: 120).",
            },
            "enabled": {
                "type": "boolean",
                "description": "Whether the job is enabled (for update).",
            },
        },
        "required": ["action"],
    },
}


def handle_cron_tool(
    tool_input: dict[str, Any],
    manager: CronManager,
) -> str:
    """Handle a cron tool call from the agent.

    Args:
        tool_input: The tool input from the LLM.
        manager: The CronManager for scheduling and persistence.

    Returns:
        JSON string with the result.
    """
    action = tool_input.get("action", "")

    try:
        if action == "list":
            return _action_list(manager)
        elif action == "add":
            return _action_add(tool_input, manager)
        elif action == "update":
            return _action_update(tool_input, manager)
        elif action == "remove":
            return _action_remove(tool_input, manager)
        elif action == "run":
            return _action_run(tool_input, manager)
        elif action == "runs":
            return _action_runs(tool_input, manager)
        else:
            return json.dumps({"error": f"Unknown action: {action}"})
    except Exception as e:
        logger.exception("Cron tool error (action=%s)", action)
        return json.dumps({"error": str(e)})


def _action_list(manager: CronManager) -> str:
    """List all jobs (managed + legacy)."""
    jobs = manager.list_jobs()
    return json.dumps(
        {
            "jobs": [_job_summary(j) for j in jobs],
            "count": len(jobs),
        }
    )


def _action_add(tool_input: dict[str, Any], manager: CronManager) -> str:
    """Create a new job and schedule it."""
    name = tool_input.get("name")
    if not name:
        return json.dumps({"error": "name is required for add"})

    schedule_kind = tool_input.get("schedule_kind")
    schedule_expr = tool_input.get("schedule_expr")
    if not schedule_kind or not schedule_expr:
        return json.dumps({"error": "schedule_kind and schedule_expr are required for add"})

    message = tool_input.get("message")
    if not message:
        return json.dumps({"error": "message is required for add"})

    tz = tool_input.get("tz", "UTC")
    schedule = Schedule(kind=schedule_kind, expr=schedule_expr, tz=tz)

    payload_kind = tool_input.get("payload_kind", "agentTurn")
    target = tool_input.get("target", "isolated")

    # systemEvent forces main target
    if payload_kind == "systemEvent":
        target = "main"

    payload_kwargs: dict[str, Any] = {
        "kind": payload_kind,
        "message": message,
        "model": tool_input.get("model"),
    }
    timeout = tool_input.get("timeout_seconds")
    if timeout is not None:
        payload_kwargs["timeout_seconds"] = timeout
    payload = Payload(**payload_kwargs)

    delivery_mode = tool_input.get("delivery_mode", "none")
    delivery = Delivery(
        mode=delivery_mode,
        channel=tool_input.get("delivery_channel"),
        url=tool_input.get("delivery_url"),
    )

    job = CronJob(
        name=name,
        schedule=schedule,
        target=target,
        payload=payload,
        delivery=delivery,
    )
    manager.add_job(job)

    return json.dumps(
        {
            "status": "created",
            "job": _job_summary(job),
        }
    )


def _action_update(tool_input: dict[str, Any], manager: CronManager) -> str:
    """Update an existing job and reschedule."""
    job_id = tool_input.get("job_id")
    if not job_id:
        return json.dumps({"error": "job_id is required for update"})

    job = manager.get_job(job_id)
    if job is None:
        return json.dumps({"error": f"Job '{job_id}' not found"})

    fields: dict[str, Any] = {}

    if "name" in tool_input and tool_input["name"]:
        fields["name"] = tool_input["name"]

    if "enabled" in tool_input:
        fields["enabled"] = tool_input["enabled"]

    # Handle schedule update
    schedule_kind = tool_input.get("schedule_kind")
    schedule_expr = tool_input.get("schedule_expr")
    if schedule_kind and schedule_expr:
        tz = tool_input.get("tz", job.schedule.tz)
        fields["schedule"] = Schedule(kind=schedule_kind, expr=schedule_expr, tz=tz).model_dump()

    if not fields:
        return json.dumps({"status": "no_changes", "job": _job_summary(job)})

    updated = manager.update_job(job_id, **fields)
    return json.dumps(
        {
            "status": "updated",
            "job": _job_summary(updated),
        }
    )


def _action_remove(tool_input: dict[str, Any], manager: CronManager) -> str:
    """Remove a job and unschedule it."""
    job_id = tool_input.get("job_id")
    if not job_id:
        return json.dumps({"error": "job_id is required for remove"})

    try:
        removed = manager.remove_job(job_id)
    except KeyError:
        return json.dumps({"error": f"Job '{job_id}' not found"})

    return json.dumps(
        {
            "status": "removed",
            "job": _job_summary(removed),
        }
    )


def _action_run(tool_input: dict[str, Any], manager: CronManager) -> str:
    """Trigger a job immediately via the CronManager."""
    job_id = tool_input.get("job_id")
    if not job_id:
        return json.dumps({"error": "job_id is required for run"})

    job = manager.get_job(job_id)
    if job is None:
        return json.dumps({"error": f"Job '{job_id}' not found"})

    manager.trigger_job(job_id)

    return json.dumps(
        {
            "status": "triggered",
            "job": _job_summary(job),
        }
    )


def _action_runs(tool_input: dict[str, Any], manager: CronManager) -> str:
    """Show run history for a job."""
    job_id = tool_input.get("job_id")
    if not job_id:
        return json.dumps({"error": "job_id is required for runs"})

    job = manager.get_job(job_id)
    runs = manager.get_runs(job_id)

    # Job may have been auto-deleted (one-shot) but runs still exist
    if job is None and not runs:
        return json.dumps({"error": f"Job '{job_id}' not found"})

    job_name = job.name if job else "(deleted)"
    return json.dumps(
        {
            "job_id": job_id,
            "job_name": job_name,
            "runs": [_run_summary(r) for r in runs],
            "count": len(runs),
        }
    )


def _job_summary(job: CronJob) -> dict[str, Any]:
    """Create a concise summary dict for a job."""
    return {
        "id": job.id,
        "name": job.name,
        "schedule": f"{job.schedule.kind}: {job.schedule.expr}",
        "tz": job.schedule.tz,
        "target": job.target,
        "enabled": job.enabled,
        "payload_kind": job.payload.kind,
        "delivery_mode": job.delivery.mode,
    }


def _run_summary(record: RunRecord) -> dict[str, Any]:
    """Create a concise summary dict for a run record."""
    summary: dict[str, Any] = {
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "status": record.status.value,
    }
    if record.error:
        summary["error"] = record.error
    return summary
