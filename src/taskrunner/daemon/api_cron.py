"""Cron job API endpoints for the Creel dashboard."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/cron", tags=["cron"])


# --- Helpers ---


def _get_tasks_dir() -> Path:
    """Resolve the tasks directory, checking common locations."""
    tasks_dir_env = os.environ.get("CREEL_TASKS_DIR")
    if tasks_dir_env:
        p = Path(tasks_dir_env)
        if p.is_dir():
            return p

    cwd_tasks = Path("tasks")
    if cwd_tasks.is_dir():
        return cwd_tasks

    creel_home = Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))
    home_tasks = creel_home / "tasks"
    if home_tasks.is_dir():
        return home_tasks

    return cwd_tasks


def _cron_history_path() -> Path:
    """Return path to the cron history JSONL file."""
    creel_home = Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))
    return creel_home / "cron-history.jsonl"


def _read_all_history() -> list[dict[str, Any]]:
    """Read all cron run records from the JSONL history file."""
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

    return runs


def _cron_to_human(expr: str) -> str:
    """Convert a 5-part cron expression to a human-readable string.

    Handles common patterns; returns the raw expression for complex ones.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return expr

    minute, hour, dom, month, dow = parts

    # Every minute
    if parts == ["*", "*", "*", "*", "*"]:
        return "Every minute"

    # Every N minutes: */N * * * *
    m = re.match(r"^\*/(\d+)$", minute)
    if m and hour == "*" and dom == "*" and month == "*" and dow == "*":
        n = int(m.group(1))
        if n == 1:
            return "Every minute"
        return f"Every {n} minutes"

    # Every hour at :MM
    if hour == "*" and dom == "*" and month == "*" and dow == "*":
        if minute.isdigit():
            mm = int(minute)
            if mm == 0:
                return "Every hour"
            return f"Every hour at :{mm:02d}"

    # Every N hours
    hm = re.match(r"^\*/(\d+)$", hour)
    if hm and minute.isdigit() and dom == "*" and month == "*" and dow == "*":
        n = int(hm.group(1))
        return f"Every {n} hours"

    # Daily at HH:MM
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*" and dow == "*":
        h = int(hour)
        mm = int(minute)
        ampm = "AM" if h < 12 else "PM"
        display_h = h % 12
        if display_h == 0:
            display_h = 12
        if mm == 0:
            return f"Every day at {display_h}:{mm:02d} {ampm}"
        return f"Every day at {display_h}:{mm:02d} {ampm}"

    # Weekly on specific day(s)
    day_names = {
        "0": "Sunday",
        "1": "Monday",
        "2": "Tuesday",
        "3": "Wednesday",
        "4": "Thursday",
        "5": "Friday",
        "6": "Saturday",
        "7": "Sunday",
    }
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*" and dow != "*":
        h = int(hour)
        mm = int(minute)
        ampm = "AM" if h < 12 else "PM"
        display_h = h % 12
        if display_h == 0:
            display_h = 12
        time_str = f"{display_h}:{mm:02d} {ampm}"

        # Single day
        if dow in day_names:
            return f"Every {day_names[dow]} at {time_str}"

        # Multiple days: 1,3,5
        if "," in dow:
            days = [day_names.get(d.strip(), d.strip()) for d in dow.split(",")]
            return f"Every {', '.join(days)} at {time_str}"

        # Range: 1-5
        rm = re.match(r"^(\d)-(\d)$", dow)
        if rm:
            start_day = day_names.get(rm.group(1), rm.group(1))
            end_day = day_names.get(rm.group(2), rm.group(2))
            return f"{start_day} through {end_day} at {time_str}"

    # Monthly on specific day
    if minute.isdigit() and hour.isdigit() and dom.isdigit() and month == "*" and dow == "*":
        h = int(hour)
        mm = int(minute)
        d = int(dom)
        ampm = "AM" if h < 12 else "PM"
        display_h = h % 12
        if display_h == 0:
            display_h = 12
        # Ordinal suffix
        if d in (1, 21, 31):
            suffix = "st"
        elif d in (2, 22):
            suffix = "nd"
        elif d in (3, 23):
            suffix = "rd"
        else:
            suffix = "th"
        return f"Monthly on the {d}{suffix} at {display_h}:{mm:02d} {ampm}"

    # Fallback: return the raw expression
    return expr


# --- Endpoints ---


@router.get("/jobs")
async def list_cron_jobs() -> list[dict[str, Any]]:
    """List all tasks that have a schedule field (cron jobs)."""
    tasks_dir = _get_tasks_dir()
    if not tasks_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []
    history = _read_all_history()

    for path in sorted(tasks_dir.glob("*.yaml")):
        try:
            with open(path) as f:
                raw = yaml.safe_load(f)
            if not isinstance(raw, dict):
                continue
            schedule = raw.get("schedule", "")
            if not schedule:
                continue

            name = raw.get("name", path.stem)
            enabled = raw.get("enabled", True)

            # Find last run and status from history
            last_run = None
            last_status = None
            for run in reversed(history):
                if run.get("job_name") == name or run.get("job_name") == path.stem:
                    last_run = run.get("finished_at")
                    last_status = run.get("status")
                    break

            results.append(
                {
                    "name": name,
                    "schedule": schedule,
                    "schedule_human": _cron_to_human(schedule),
                    "next_run": None,  # Would need APScheduler access for real next-run time
                    "last_run": last_run,
                    "last_status": last_status,
                    "enabled": enabled,
                }
            )
        except Exception:
            continue

    return results


@router.post("/jobs/{name}/toggle")
async def toggle_cron_job(name: str) -> dict[str, Any]:
    """Toggle the enabled field of a cron job's YAML file."""
    tasks_dir = _get_tasks_dir()
    path = tasks_dir / f"{name}.yaml"

    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Job '{name}' not found")

    raw_yaml_str = path.read_text()
    try:
        raw = yaml.safe_load(raw_yaml_str)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid YAML: {exc}")

    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="Task file is not a YAML mapping")

    current_enabled = raw.get("enabled", True)
    new_enabled = not current_enabled

    # Create .bak backup before modifying
    bak_path = path.with_suffix(".yaml.bak")
    shutil.copy2(path, bak_path)

    # Update via regex to preserve YAML formatting
    if re.search(r"^enabled\s*:", raw_yaml_str, re.MULTILINE):
        updated = re.sub(
            r"^(enabled\s*:\s*).*$",
            rf"\g<1>{str(new_enabled).lower()}",
            raw_yaml_str,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        # No enabled field exists — append it
        updated = raw_yaml_str.rstrip() + f"\nenabled: {str(new_enabled).lower()}\n"

    path.write_text(updated)

    return {
        "name": name,
        "enabled": new_enabled,
        "schedule": raw.get("schedule", ""),
    }


@router.get("/history")
async def get_cron_history(
    job: str | None = Query(None, description="Filter by job name"),
    status: str | None = Query(None, description="Filter by status: success|failed|timeout"),
    limit: int = Query(50, ge=1, le=500, description="Max records to return"),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
) -> dict[str, Any]:
    """Return paginated cron run history from the JSONL file."""
    all_runs = _read_all_history()

    # Apply filters
    filtered = all_runs
    if job:
        filtered = [r for r in filtered if r.get("job_name") == job]
    if status:
        filtered = [r for r in filtered if r.get("status") == status]

    # Most recent first
    filtered.reverse()

    total = len(filtered)
    page = filtered[offset : offset + limit]

    return {
        "runs": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/history/{run_id}")
async def get_cron_run_detail(run_id: str) -> dict[str, Any]:
    """Return full detail of a single cron run by run_id."""
    all_runs = _read_all_history()

    for run in all_runs:
        if run.get("run_id") == run_id:
            return run

    raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
