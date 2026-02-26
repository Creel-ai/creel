"""Dashboard API endpoints for the Creel web UI."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from taskrunner.models import load_all_tasks

router = APIRouter(prefix="/api", tags=["dashboard"])


def _get_tasks_dir() -> Path:
    """Resolve the tasks directory, checking common locations."""
    # Check environment variable first
    tasks_dir_env = os.environ.get("CREEL_TASKS_DIR")
    if tasks_dir_env:
        p = Path(tasks_dir_env)
        if p.is_dir():
            return p

    # Check relative to CWD
    cwd_tasks = Path("tasks")
    if cwd_tasks.is_dir():
        return cwd_tasks

    # Check CREEL_HOME
    creel_home = Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))
    home_tasks = creel_home / "tasks"
    if home_tasks.is_dir():
        return home_tasks

    return cwd_tasks  # default even if missing


def _cron_history_path() -> Path:
    """Return path to the cron history JSONL file."""
    creel_home = Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))
    return creel_home / "cron-history.jsonl"


def _read_recent_runs(limit: int = 5) -> list[dict[str, Any]]:
    """Read the most recent cron run records from JSONL history."""
    import json

    history_path = _cron_history_path()
    if not history_path.is_file():
        return []

    runs: list[dict[str, Any]] = []
    try:
        with open(history_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        runs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []

    # Return the last N records (most recent)
    return runs[-limit:]


@router.get("/status")
async def dashboard_status(request: Request) -> dict[str, Any]:
    """Return daemon status, agent info, and summary stats for the overview page."""
    service = request.app.state.service

    # Daemon info from service status
    svc_status = service.status()
    pid = os.getpid()
    uptime = svc_status.get("uptime_seconds", 0)
    started_at = svc_status.get("started_at", 0)

    # Socket path
    creel_home = Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))
    socket_path = str(creel_home / "daemon.sock")

    # Agent info from the agent definition
    agent_def = service._agent_def
    agent_info = {
        "name": "creel",
        "model": agent_def.llm.model,
        "provider": "anthropic",
    }

    # Channels with enabled/connected status
    channels_raw = svc_status.get("channels", [])
    channels = []
    for ch in channels_raw:
        channels.append({
            "name": ch["name"],
            "enabled": True,
            "connected": ch.get("running", False),
        })

    # If no channels are registered yet, enumerate configured ones
    if not channels:
        for ch_id in agent_def.channels.configured_channels():
            channels.append({
                "name": ch_id,
                "enabled": True,
                "connected": False,
            })

    # Task stats
    tasks_dir = _get_tasks_dir()
    total_tasks = 0
    scheduled_tasks = 0
    try:
        task_defs = load_all_tasks(tasks_dir)
        total_tasks = len(task_defs)
        scheduled_tasks = sum(1 for t in task_defs if t.schedule)
    except (FileNotFoundError, Exception):
        pass

    # Cron info
    scheduler_running = svc_status.get("scheduler", {}).get("running", False)

    # Count cron jobs (tasks with schedules)
    enabled_cron_jobs = scheduled_tasks
    total_cron_jobs = scheduled_tasks

    # Next run - we can't easily determine this without the scheduler internals,
    # so return None and let the frontend handle it
    next_run = None

    # Recent runs from history
    recent_runs_raw = _read_recent_runs(5)
    recent_runs = []
    for run in recent_runs_raw:
        recent_runs.append({
            "task_name": run.get("job_name", run.get("task_name", "unknown")),
            "status": run.get("status", "unknown"),
            "finished_at": run.get("finished_at"),
            "duration_ms": run.get("duration_ms"),
        })

    return {
        "daemon": {
            "running": True,
            "pid": pid,
            "uptime_seconds": uptime,
            "socket": socket_path,
        },
        "agent": agent_info,
        "channels": channels,
        "tasks": {
            "total": total_tasks,
            "scheduled": scheduled_tasks,
        },
        "cron": {
            "enabled_jobs": enabled_cron_jobs,
            "total_jobs": total_cron_jobs,
            "next_run": next_run,
        },
        "recent_runs": recent_runs,
    }
