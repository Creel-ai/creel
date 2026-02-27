"""File browser API endpoints for the Creel dashboard."""

from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/files", tags=["files"])

# Files/dirs to exclude from the tree listing
_EXCLUDE_NAMES = {
    "__pycache__",
    ".pyc",
    "daemon.sock",
    "daemon.pid",
    ".git",
    ".deleted",
}
_EXCLUDE_SUFFIXES = {".bak", ".pyc"}

# Files that cannot be deleted
_PROTECTED_FILES = {"agent.yaml", "daemon.pid", "daemon.sock"}

# Max depth for recursive tree
_MAX_DEPTH = 5

# Max file size for content retrieval (1 MB)
_MAX_FILE_SIZE = 1 * 1024 * 1024


def _creel_home() -> Path:
    """Return the CREEL_HOME directory path."""
    return Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))


def _validate_path(raw_path: str) -> Path:
    """Validate and resolve a user-supplied path to be within CREEL_HOME.

    Rejects any path traversal attempts. Returns the resolved absolute path.
    """
    home = _creel_home().resolve()

    # Reject obvious traversal
    if ".." in raw_path.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    # Build and resolve the target path
    target = (home / raw_path).resolve()

    # Ensure the resolved path is within CREEL_HOME (or is CREEL_HOME itself)
    if target != home and home not in target.parents:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    return target


def _should_exclude(name: str) -> bool:
    """Check if a file/dir name should be excluded from listings."""
    if name in _EXCLUDE_NAMES:
        return True
    if name.startswith(".") and name in {".git", ".deleted"}:
        return True
    for suffix in _EXCLUDE_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def _build_tree(path: Path, depth: int = 0) -> dict[str, Any] | None:
    """Recursively build a directory tree structure."""
    if depth > _MAX_DEPTH:
        return None

    name = path.name
    if _should_exclude(name):
        return None

    try:
        stat = path.stat()
    except OSError:
        return None

    modified_at = datetime.datetime.fromtimestamp(
        stat.st_mtime, tz=datetime.timezone.utc
    ).isoformat()

    if path.is_file():
        return {
            "name": name,
            "path": str(path.relative_to(_creel_home().resolve())),
            "type": "file",
            "size_bytes": stat.st_size,
            "modified_at": modified_at,
        }

    if path.is_dir():
        children: list[dict[str, Any]] = []
        try:
            for child in sorted(path.iterdir()):
                child_node = _build_tree(child, depth + 1)
                if child_node is not None:
                    children.append(child_node)
        except PermissionError:
            pass

        return {
            "name": name,
            "path": str(path.relative_to(_creel_home().resolve())),
            "type": "dir",
            "size_bytes": 0,
            "modified_at": modified_at,
            "children": children,
        }

    return None


def _is_text_file(path: Path) -> bool:
    """Heuristic check if a file is likely text (not binary)."""
    text_extensions = {
        ".yaml",
        ".yml",
        ".json",
        ".md",
        ".txt",
        ".toml",
        ".cfg",
        ".ini",
        ".log",
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".conf",
        ".env",
        ".csv",
        ".xml",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".jsonl",
    }
    if path.suffix.lower() in text_extensions:
        return True
    # For unknown extensions, try reading a small sample
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
        # Check for null bytes (binary indicator)
        return b"\x00" not in chunk
    except OSError:
        return False


# --- Request models ---


class FileWriteRequest(BaseModel):
    content: str


# --- Endpoints ---


@router.get("/tree")
async def get_file_tree() -> dict[str, Any]:
    """Return a recursive directory tree of ~/.creel/."""
    home = _creel_home()
    if not home.is_dir():
        return {
            "name": home.name,
            "path": "",
            "type": "dir",
            "size_bytes": 0,
            "modified_at": None,
            "children": [],
        }

    tree = _build_tree(home.resolve())
    if tree is None:
        return {
            "name": home.name,
            "path": "",
            "type": "dir",
            "size_bytes": 0,
            "modified_at": None,
            "children": [],
        }

    # Root path should be empty string (relative to itself)
    tree["path"] = ""
    return tree


@router.get("/{file_path:path}")
async def get_file(file_path: str) -> dict[str, Any]:
    """Return the contents of a file within ~/.creel/."""
    if not file_path:
        raise HTTPException(status_code=400, detail="File path is required")

    target = _validate_path(file_path)

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    stat = target.stat()
    modified_at = datetime.datetime.fromtimestamp(
        stat.st_mtime, tz=datetime.timezone.utc
    ).isoformat()

    if stat.st_size > _MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({stat.st_size} bytes). Maximum is {_MAX_FILE_SIZE} bytes.",
        )

    if not _is_text_file(target):
        return {
            "path": file_path,
            "content": None,
            "binary": True,
            "size_bytes": stat.st_size,
            "modified_at": modified_at,
        }

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {
            "path": file_path,
            "content": None,
            "binary": True,
            "size_bytes": stat.st_size,
            "modified_at": modified_at,
        }

    return {
        "path": file_path,
        "content": content,
        "binary": False,
        "size_bytes": stat.st_size,
        "modified_at": modified_at,
    }


@router.put("/{file_path:path}")
async def update_file(file_path: str, req: FileWriteRequest) -> dict[str, Any]:
    """Write content to an existing file within ~/.creel/, creating .bak backup."""
    if not file_path:
        raise HTTPException(status_code=400, detail="File path is required")

    target = _validate_path(file_path)

    # Create parent directories if they don't exist
    target.parent.mkdir(parents=True, exist_ok=True)

    # Create .bak backup if the file already exists
    if target.is_file():
        bak_path = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, bak_path)

    target.write_text(req.content, encoding="utf-8")

    stat = target.stat()
    modified_at = datetime.datetime.fromtimestamp(
        stat.st_mtime, tz=datetime.timezone.utc
    ).isoformat()

    return {
        "path": file_path,
        "size_bytes": stat.st_size,
        "modified_at": modified_at,
    }


@router.post("/{file_path:path}", status_code=201)
async def create_file(file_path: str, req: FileWriteRequest) -> dict[str, Any]:
    """Create a new file within ~/.creel/. Returns 409 if file already exists."""
    if not file_path:
        raise HTTPException(status_code=400, detail="File path is required")

    target = _validate_path(file_path)

    if target.exists():
        raise HTTPException(status_code=409, detail=f"File already exists: {file_path}")

    # Create parent directories if they don't exist
    target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text(req.content, encoding="utf-8")

    stat = target.stat()
    modified_at = datetime.datetime.fromtimestamp(
        stat.st_mtime, tz=datetime.timezone.utc
    ).isoformat()

    return {
        "path": file_path,
        "size_bytes": stat.st_size,
        "modified_at": modified_at,
    }


@router.delete("/{file_path:path}")
async def delete_file(file_path: str) -> dict[str, Any]:
    """Soft-delete a file by moving it to .trash/ within ~/.creel/."""
    if not file_path:
        raise HTTPException(status_code=400, detail="File path is required")

    target = _validate_path(file_path)

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    # Check if the file is protected
    if target.name in _PROTECTED_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete protected file: {target.name}",
        )

    home = _creel_home().resolve()
    trash_dir = home / ".trash"
    trash_dir.mkdir(exist_ok=True)

    # Preserve relative path structure in trash
    rel_path = target.relative_to(home)
    trash_dest = trash_dir / rel_path

    # Add timestamp suffix if a file with the same name already exists in trash
    if trash_dest.exists():
        ts = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        trash_dest = trash_dest.with_stem(f"{trash_dest.stem}.{ts}")

    trash_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(trash_dest))

    return {
        "status": "deleted",
        "path": file_path,
        "moved_to": str(trash_dest.relative_to(home)),
    }
