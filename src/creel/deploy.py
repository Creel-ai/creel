"""Agent deployment controls — versioning, deploy, and rollback.

Treats agent configuration changes like software releases: each deploy
creates a versioned snapshot so you can roll back atomically.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class DeploymentRecord(BaseModel):
    """A single entry in the deployment history log."""

    version: int
    tag: str | None = None
    timestamp: str
    config_hash: str
    message: str = ""

    @property
    def label(self) -> str:
        """Human-readable label: tag if set, else 'v<version>'."""
        return self.tag or f"v{self.version}"


class DeploymentHistory(BaseModel):
    """Persistent deployment history stored as JSON."""

    records: list[DeploymentRecord] = Field(default_factory=list)
    active_version: int | None = None


def _hash_content(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


def _read_yaml_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _snapshot_dir(deploy_dir: Path, version: int) -> Path:
    return deploy_dir / "snapshots" / str(version)


def _history_path(deploy_dir: Path) -> Path:
    return deploy_dir / "history.json"


def _load_history(deploy_dir: Path) -> DeploymentHistory:
    hp = _history_path(deploy_dir)
    if hp.exists():
        return DeploymentHistory(**json.loads(hp.read_text()))
    return DeploymentHistory()


def _save_history(deploy_dir: Path, history: DeploymentHistory) -> None:
    hp = _history_path(deploy_dir)
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps(history.model_dump(), indent=2) + "\n")


def _collect_config_files(creel_home: Path) -> dict[str, Path]:
    """Collect all config files that form a deployment snapshot.

    Returns a mapping of relative-path -> absolute-path for files to snapshot.
    """
    files: dict[str, Path] = {}

    agent_yaml = creel_home / "agent.yaml"
    if agent_yaml.exists():
        files["agent.yaml"] = agent_yaml

    policies_dir = creel_home / "policies"
    if policies_dir.is_dir():
        for p in sorted(policies_dir.rglob("*.yaml")):
            files[str(p.relative_to(creel_home))] = p

    tasks_dir = creel_home / "tasks"
    if tasks_dir.is_dir():
        for p in sorted(tasks_dir.rglob("*.yaml")):
            files[str(p.relative_to(creel_home))] = p

    return files


def _snapshot_hash(creel_home: Path) -> str:
    """Compute a deterministic hash of all config files."""
    files = _collect_config_files(creel_home)
    h = hashlib.sha256()
    for rel in sorted(files):
        content = files[rel].read_bytes()
        h.update(rel.encode())
        h.update(content)
    return h.hexdigest()[:16]


def validate_config(config_path: Path) -> list[str]:
    """Validate the agent config at *config_path*.

    Returns a list of error strings (empty == valid).
    """
    errors: list[str] = []

    if not config_path.exists():
        errors.append(f"Config file not found: {config_path}")
        return errors

    try:
        raw_text = config_path.read_text()
    except OSError as exc:
        errors.append(f"Cannot read config: {exc}")
        return errors

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        errors.append(f"YAML parse error: {exc}")
        return errors

    if not isinstance(raw, dict):
        errors.append(f"Config must be a YAML mapping, got {type(raw).__name__}")
        return errors

    from creel.models import AgentDefinition

    try:
        AgentDefinition(**raw)
    except Exception as exc:
        errors.append(f"Schema validation error: {exc}")

    return errors


def create_snapshot(
    creel_home: Path,
    deploy_dir: Path,
    tag: str | None = None,
    message: str = "",
) -> DeploymentRecord:
    """Create a versioned snapshot of the current configuration.

    Copies all config files (agent.yaml, policies/, tasks/) into
    ``deploy_dir/snapshots/<version>/``.
    """
    history = _load_history(deploy_dir)
    next_version = (history.records[-1].version + 1) if history.records else 1

    if tag:
        for rec in history.records:
            if rec.tag == tag:
                raise ValueError(f"Tag '{tag}' already exists (version {rec.version})")

    config_hash = _snapshot_hash(creel_home)

    snap = _snapshot_dir(deploy_dir, next_version)
    snap.mkdir(parents=True, exist_ok=True)

    files = _collect_config_files(creel_home)
    for rel, src in files.items():
        dest = snap / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    record = DeploymentRecord(
        version=next_version,
        tag=tag,
        timestamp=datetime.now(UTC).isoformat(),
        config_hash=config_hash,
        message=message,
    )
    history.records.append(record)
    history.active_version = next_version
    _save_history(deploy_dir, history)
    return record


def deploy(creel_home: Path, deploy_dir: Path, version: int) -> DeploymentRecord:
    """Activate a previously-snapshotted config version.

    Atomically replaces config files in *creel_home* with the snapshot
    contents by writing to a temp location and renaming.
    """
    history = _load_history(deploy_dir)
    record = None
    for rec in history.records:
        if rec.version == version:
            record = rec
            break
    if record is None:
        raise ValueError(f"Version {version} not found in deployment history")

    snap = _snapshot_dir(deploy_dir, version)
    if not snap.exists():
        raise ValueError(f"Snapshot directory missing for version {version}")

    # Atomic switch: write files from snapshot back into creel_home
    for rel_path in sorted(snap.rglob("*")):
        if rel_path.is_file():
            rel = rel_path.relative_to(snap)
            dest = creel_home / rel
            # Write to temp then rename for atomicity
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rel_path, tmp)
            tmp.rename(dest)

    history.active_version = version
    _save_history(deploy_dir, history)
    return record


def rollback(
    creel_home: Path,
    deploy_dir: Path,
    target_tag: str | None = None,
) -> DeploymentRecord:
    """Roll back to a previous deployment.

    If *target_tag* is given, roll back to that tagged version.
    Otherwise, roll back to the version before the currently active one.
    """
    history = _load_history(deploy_dir)
    if not history.records:
        raise ValueError("No deployment history — nothing to roll back to")

    if target_tag is not None:
        target_rec = None
        for rec in history.records:
            if rec.tag == target_tag or f"v{rec.version}" == target_tag:
                target_rec = rec
                break
        if target_rec is None:
            raise ValueError(f"No deployment found with tag or version '{target_tag}'")
        return deploy(creel_home, deploy_dir, target_rec.version)

    # Roll back to the version before the active one
    if history.active_version is None:
        raise ValueError("No active deployment — nothing to roll back from")

    active_idx = None
    for i, rec in enumerate(history.records):
        if rec.version == history.active_version:
            active_idx = i
            break
    if active_idx is None or active_idx == 0:
        raise ValueError("Already at the earliest version — cannot roll back further")

    prev = history.records[active_idx - 1]
    return deploy(creel_home, deploy_dir, prev.version)


def get_history(deploy_dir: Path) -> list[dict[str, Any]]:
    """Return deployment history as a list of dicts for display."""
    history = _load_history(deploy_dir)
    rows: list[dict[str, Any]] = []
    for rec in history.records:
        rows.append(
            {
                "version": rec.version,
                "tag": rec.tag or "",
                "timestamp": rec.timestamp,
                "hash": rec.config_hash,
                "active": rec.version == history.active_version,
                "message": rec.message,
            }
        )
    return rows
