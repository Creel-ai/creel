"""Task CRUD API endpoints for the Creel dashboard."""

from __future__ import annotations

import datetime
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from taskrunner.models import TaskDefinition, load_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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


# --- Request/Response models ---


class TaskSummary(BaseModel):
    name: str
    description: str = ""
    schedule: str = ""
    enabled: bool = True
    last_modified: str | None = None
    file_path: str = ""


class TaskDetail(BaseModel):
    name: str
    description: str = ""
    schedule: str = ""
    prompt: str = ""
    output_type: str = ""
    output_to: str = ""
    model: str = ""
    max_tokens: int = 300
    mode: str = "simple"
    enabled: bool = True
    raw_yaml: str = ""
    file_path: str = ""
    last_modified: str | None = None


class TaskCreateRequest(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = ""
    schedule: str = "0 0 * * *"
    prompt: str = ""
    output_type: str = "stdout"
    output_to: str = ""
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 300
    mode: str = "simple"
    enabled: bool = True
    raw_yaml: str | None = None


class TaskRunResponse(BaseModel):
    run_id: str
    task_name: str
    status: str = "started"


# --- Helpers ---


def _task_file_path(tasks_dir: Path, name: str) -> Path:
    """Return the YAML file path for a task name."""
    return tasks_dir / f"{name}.yaml"


def _parse_task_yaml(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract dashboard-friendly fields from raw task YAML dict."""
    output = raw.get("output", {})
    llm = raw.get("llm", {})
    return {
        "description": raw.get("description", ""),
        "schedule": raw.get("schedule", ""),
        "prompt": raw.get("prompt", ""),
        "output_type": output.get("type", "") if isinstance(output, dict) else "",
        "output_to": output.get("to", "") if isinstance(output, dict) else "",
        "model": llm.get("model", "") if isinstance(llm, dict) else "",
        "max_tokens": llm.get("max_tokens", 300) if isinstance(llm, dict) else 300,
        "mode": raw.get("mode", "simple"),
        "enabled": raw.get("enabled", True),
    }


def _build_task_yaml(req: TaskCreateRequest) -> dict[str, Any]:
    """Build a YAML-serialisable dict from a create/update request."""
    data: dict[str, Any] = {
        "name": req.name,
        "schedule": req.schedule,
        "prompt": req.prompt,
        "output": {
            "type": req.output_type,
            "to": req.output_to,
        },
        "llm": {
            "model": req.model,
            "max_tokens": req.max_tokens,
        },
        "mode": req.mode,
    }
    if req.description:
        data["description"] = req.description
    if not req.enabled:
        data["enabled"] = False
    return data


def _validate_raw_yaml_name(raw: dict[str, Any], expected_name: str) -> None:
    """Require raw YAML task name to match the task file identity."""
    raw_name = raw.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise HTTPException(
            status_code=400, detail="Task YAML must include a non-empty 'name' field"
        )
    if raw_name != expected_name:
        raise HTTPException(
            status_code=400,
            detail=f"Task YAML name '{raw_name}' must match task name '{expected_name}'",
        )


# --- Endpoints ---


@router.get("")
async def list_tasks() -> list[TaskSummary]:
    """List all task definitions from the tasks directory."""
    tasks_dir = _get_tasks_dir()
    if not tasks_dir.is_dir():
        return []

    results: list[TaskSummary] = []
    for path in sorted(tasks_dir.glob("*.yaml")):
        try:
            with open(path) as f:
                raw = yaml.safe_load(f)
            if not isinstance(raw, dict):
                continue
            stat = path.stat()
            results.append(
                TaskSummary(
                    name=raw.get("name", path.stem),
                    description=raw.get("description", ""),
                    schedule=raw.get("schedule", ""),
                    enabled=raw.get("enabled", True),
                    last_modified=datetime.datetime.fromtimestamp(
                        stat.st_mtime, tz=datetime.timezone.utc
                    ).isoformat(),
                    file_path=str(path),
                )
            )
        except Exception:
            continue

    return results


@router.get("/{name}")
async def get_task(name: str) -> TaskDetail:
    """Return full task details including raw YAML."""
    tasks_dir = _get_tasks_dir()
    path = _task_file_path(tasks_dir, name)

    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Task '{name}' not found")

    raw_yaml_str = path.read_text()
    try:
        raw = yaml.safe_load(raw_yaml_str)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid YAML: {exc}")

    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="Task file is not a YAML mapping")

    parsed = _parse_task_yaml(raw)
    stat = path.stat()

    return TaskDetail(
        name=raw.get("name", name),
        raw_yaml=raw_yaml_str,
        file_path=str(path),
        last_modified=datetime.datetime.fromtimestamp(
            stat.st_mtime, tz=datetime.timezone.utc
        ).isoformat(),
        **parsed,
    )


@router.post("", status_code=201)
async def create_task(req: TaskCreateRequest) -> TaskDetail:
    """Create a new task YAML file."""
    tasks_dir = _get_tasks_dir()
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = _task_file_path(tasks_dir, req.name)

    if path.exists():
        raise HTTPException(status_code=409, detail=f"Task '{req.name}' already exists")

    # If raw_yaml is provided, use it directly; otherwise build from fields
    if req.raw_yaml:
        raw_yaml_str = req.raw_yaml
        try:
            raw = yaml.safe_load(raw_yaml_str)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        _validate_raw_yaml_name(raw, req.name)
        # Validate by loading as TaskDefinition
        try:
            TaskDefinition(**raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Validation error: {exc}")
    else:
        data = _build_task_yaml(req)
        # Validate by loading as TaskDefinition
        try:
            TaskDefinition(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Validation error: {exc}")
        raw_yaml_str = yaml.dump(data, default_flow_style=False, sort_keys=False)

    path.write_text(raw_yaml_str)

    # Return the created task detail
    return await get_task(req.name)


@router.put("/{name}")
async def update_task(name: str, req: TaskCreateRequest) -> TaskDetail:
    """Update an existing task YAML file."""
    if req.name != name:
        raise HTTPException(
            status_code=400,
            detail=f"Request task name '{req.name}' must match route name '{name}'",
        )

    tasks_dir = _get_tasks_dir()
    path = _task_file_path(tasks_dir, name)

    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Task '{name}' not found")

    if req.raw_yaml:
        raw_yaml_str = req.raw_yaml
        try:
            raw = yaml.safe_load(raw_yaml_str)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        _validate_raw_yaml_name(raw, name)
        try:
            TaskDefinition(**raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Validation error: {exc}")
    else:
        data = _build_task_yaml(req)
        try:
            TaskDefinition(**data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Validation error: {exc}")
        raw_yaml_str = yaml.dump(data, default_flow_style=False, sort_keys=False)

    # Create .bak backup before overwriting
    bak_path = path.with_suffix(".yaml.bak")
    shutil.copy2(path, bak_path)

    path.write_text(raw_yaml_str)

    return await get_task(name)


@router.delete("/{name}")
async def delete_task(name: str) -> dict[str, str]:
    """Soft-delete a task by moving its file to .deleted/ subdirectory."""
    tasks_dir = _get_tasks_dir()
    path = _task_file_path(tasks_dir, name)

    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Task '{name}' not found")

    deleted_dir = tasks_dir / ".deleted"
    deleted_dir.mkdir(exist_ok=True)

    dest = deleted_dir / path.name
    # Add timestamp suffix if a file with the same name already exists in .deleted/
    if dest.exists():
        ts = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = deleted_dir / f"{path.stem}.{ts}.yaml"

    shutil.move(str(path), str(dest))

    return {"status": "deleted", "task": name, "moved_to": str(dest)}


@router.post("/{name}/run", status_code=202)
async def run_task_endpoint(name: str) -> TaskRunResponse:
    """Trigger a task run in a background thread."""
    tasks_dir = _get_tasks_dir()
    path = _task_file_path(tasks_dir, name)

    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Task '{name}' not found")

    # Validate the task is loadable
    try:
        load_task(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Task load error: {exc}")

    run_id = str(uuid.uuid4())

    def _run():
        from taskrunner.orchestrator import run_task

        try:
            run_task(str(path))
        except Exception:
            pass  # Errors are logged by the orchestrator

    thread = threading.Thread(target=_run, name=f"task-run-{name}-{run_id}", daemon=True)
    thread.start()

    return TaskRunResponse(run_id=run_id, task_name=name, status="started")
