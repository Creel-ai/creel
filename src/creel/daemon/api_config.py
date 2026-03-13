"""Config API endpoints for the Creel dashboard."""

from __future__ import annotations

import os
import shutil
import signal
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from creel.models import AgentDefinition

router = APIRouter(prefix="/api/config", tags=["config"])


def _find_agent_config() -> Path:
    """Locate the agent.yaml config file."""
    # Check CWD first (matches CLI default)
    cwd_config = Path("agent.yaml")
    if cwd_config.is_file():
        return cwd_config

    # Check CREEL_HOME
    creel_home = Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))
    home_config = creel_home / "agent.yaml"
    if home_config.is_file():
        return home_config

    return cwd_config  # default even if missing


# --- Request models ---


class ConfigUpdateRequest(BaseModel):
    config_json: dict[str, Any] | None = Field(None, alias="json")
    raw_yaml: str | None = None


# --- Endpoints ---


@router.get("")
async def get_config() -> dict[str, Any]:
    """Return parsed agent.yaml as JSON plus raw_yaml field."""
    config_path = _find_agent_config()

    if not config_path.is_file():
        raise HTTPException(status_code=404, detail="agent.yaml not found")

    raw_yaml_str = config_path.read_text()
    try:
        raw = yaml.safe_load(raw_yaml_str)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="Config file is not a YAML mapping")

    return {
        "config": raw,
        "raw_yaml": raw_yaml_str,
    }


@router.put("")
async def update_config(req: ConfigUpdateRequest) -> dict[str, Any]:
    """Update agent.yaml from JSON or raw YAML. Validates against AgentDefinition model."""
    config_path = _find_agent_config()

    if not config_path.is_file():
        raise HTTPException(status_code=404, detail="agent.yaml not found")

    if req.raw_yaml is not None:
        raw_yaml_str = req.raw_yaml
        try:
            raw = yaml.safe_load(raw_yaml_str)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
    elif req.config_json is not None:
        raw = req.config_json
        raw_yaml_str = yaml.dump(raw, default_flow_style=False, sort_keys=False)
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'json' or 'raw_yaml' field",
        )

    # Validate against the Pydantic model
    try:
        AgentDefinition(**raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Validation error: {exc}") from exc

    # Create .bak backup before overwriting
    bak_path = config_path.with_suffix(".yaml.bak")
    shutil.copy2(config_path, bak_path)

    config_path.write_text(raw_yaml_str)

    return {
        "status": "saved",
        "config": raw,
        "raw_yaml": raw_yaml_str,
    }


@router.post("/reload")
async def reload_config(request: Request) -> dict[str, Any]:
    """Hot-reload agent config from disk without restarting the daemon."""
    import asyncio

    service = request.app.state.service
    result = await asyncio.to_thread(service.reload_config)

    if not result.success:
        raise HTTPException(status_code=400, detail=result.error or "Reload failed")

    response: dict[str, Any] = {
        "status": "reloaded" if result.changes else "unchanged",
        "summary": result.summary(),
        "changes": [
            {"field": c.field, "old": str(c.old_value)[:200], "new": str(c.new_value)[:200]}
            for c in result.changes
        ],
    }
    if result.non_reloadable:
        response["non_reloadable"] = [
            {"field": c.field, "message": "Requires restart"} for c in result.non_reloadable
        ]

    return response


@router.post("/apply", status_code=202)
async def apply_config(request: Request) -> dict[str, Any]:
    """Apply config changes — hot-reload first, fall back to SIGHUP restart.

    Attempts a hot-reload. If there are non-reloadable changes, also sends
    SIGHUP so the daemon restarts to pick those up.
    """
    import asyncio

    service = request.app.state.service
    result = await asyncio.to_thread(service.reload_config)

    if not result.success:
        # Reload failed — fall back to restart signal
        try:
            os.kill(os.getpid(), signal.SIGHUP)
        except OSError:
            pass
        return {"status": "restart_requested", "reason": result.error}

    response: dict[str, Any] = {
        "status": "reloaded" if result.changes else "unchanged",
        "summary": result.summary(),
    }

    if result.non_reloadable:
        try:
            os.kill(os.getpid(), signal.SIGHUP)
        except OSError:
            pass
        response["status"] = "restart_requested"
        response["reason"] = "Non-reloadable changes require restart"

    return response


@router.get("/schema")
async def get_config_schema() -> dict[str, Any]:
    """Return JSON Schema derived from the AgentDefinition Pydantic model."""
    return AgentDefinition.model_json_schema()
