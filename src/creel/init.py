"""creel init — scaffold ~/.creel/ directory structure."""

from __future__ import annotations

import importlib.resources
import shutil
from pathlib import Path

from creel import paths


def _copy_template(template_name: str, dest: Path, *, force: bool = False) -> bool:
    """Copy a bundled template file to *dest*.

    Returns True if the file was written, False if skipped (already exists
    and *force* is False).
    """
    if dest.exists() and not force:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)

    templates = importlib.resources.files("creel") / "templates"
    source = templates / template_name
    dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def init(*, force: bool = False) -> list[str]:
    """Create the ~/.creel/ directory structure and copy starter templates.

    Returns a list of human-readable status lines.
    """
    home = paths.creel_home()
    lines: list[str] = []

    # Create directories
    for dir_fn in (
        paths.policies_dir,
        paths.secrets_dir,
        paths.sessions_dir,
        paths.workspace_dir,
        paths.cron_dir,
    ):
        d = dir_fn()
        d.mkdir(parents=True, exist_ok=True)
        lines.append(f"  {'exists' if d.exists() else 'created'}: {d}")

    # Copy templates
    wrote = _copy_template("agent.yaml", paths.agent_config(), force=force)
    lines.append(f"  {'wrote' if wrote else 'exists (use --force to overwrite)'}: {paths.agent_config()}")

    wrote = _copy_template(
        "policies/default.yaml",
        paths.policies_dir() / "default.yaml",
        force=force,
    )
    lines.append(f"  {'wrote' if wrote else 'exists (use --force to overwrite)'}: {paths.policies_dir() / 'default.yaml'}")

    return lines


def migrate(repo_root: Path, *, force: bool = False) -> list[str]:
    """Copy existing repo-based config into ~/.creel/.

    Copies agent.yaml, policies/, secrets/, workspace/, and sessions/
    from *repo_root* into the creel home directory.

    Returns a list of human-readable status lines.
    """
    # Run init first to ensure directories exist
    lines = init(force=force)
    lines.append("")
    lines.append("Migration:")

    copies: list[tuple[Path, Path]] = []

    # agent.yaml
    src = repo_root / "agent.yaml"
    if src.exists():
        copies.append((src, paths.agent_config()))

    # Directory trees to copy
    dir_mappings = [
        ("policies", paths.policies_dir()),
        ("secrets", paths.secrets_dir()),
        ("workspace", paths.workspace_dir()),
        ("sessions", paths.sessions_dir()),
    ]

    for dirname, dest_dir in dir_mappings:
        src_dir = repo_root / dirname
        if not src_dir.is_dir():
            continue
        for src_file in src_dir.rglob("*"):
            if src_file.is_file():
                rel = src_file.relative_to(src_dir)
                copies.append((src_file, dest_dir / rel))

    for src_file, dest_file in copies:
        if dest_file.exists() and not force:
            lines.append(f"  skipped (exists): {dest_file}")
            continue
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        lines.append(f"  copied: {src_file} -> {dest_file}")

    if not copies:
        lines.append("  nothing to migrate (no config files found at repo root)")

    return lines
