"""OpenClaw -> Creel migration helpers.

This module provides a phased migrator:

- Phase 1: workspace files, memory files, and conversation history
- Phase 2: config/integration/tool mapping and cron -> task YAML generation
- Phase 3: skill classification + hybrid migration artifacts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PHASES = ("1", "2")
DEFAULT_HISTORY_DIRS = (
    "sessions",
    "history",
    "conversation_history",
    "conversations",
    "chat_history",
    "chats",
    "threads",
)
DEFAULT_TOP_LEVEL_HISTORY_FILES = (
    "history.json",
    "history.jsonl",
    "conversations.json",
    "conversations.jsonl",
    "sessions.json",
    "sessions.jsonl",
    "chat_history.json",
    "chat_history.jsonl",
)
DEFAULT_CONFIG_CANDIDATES = (
    "openclaw.yaml",
    "openclaw.yml",
    "openclaw.json",
    "config/openclaw.yaml",
    "config/openclaw.yml",
    "config/openclaw.json",
    "config.yaml",
    "config.yml",
    "config.json",
)
SKILLS_BEGIN = "<!-- OPENCLAW_SKILLS_BEGIN -->"
SKILLS_END = "<!-- OPENCLAW_SKILLS_END -->"

_NETWORK_EXECUTORS = {
    "brave_search",
    "browser",
    "fetch_url",
    "gcal",
    "gcal_write",
    "gmail_readonly",
    "gmail_send",
    "gmail_modify",
    "drive",
    "drive_write",
    "weather",
}

_EXECUTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "brave_search": ("brave", "web search", "search web", "internet search"),
    "browser": ("playwright", "browser", "navigate", "screenshot website"),
    "gcal": ("calendar", "schedule", "events", "meeting list"),
    "gcal_write": ("create event", "add event", "calendar write"),
    "gmail_readonly": ("gmail", "inbox", "read email", "email search"),
    "gmail_send": ("send email", "compose email", "draft email"),
    "gmail_modify": ("trash email", "mark read", "archive email", "delete email"),
    "drive": ("google drive", "drive search", "drive files"),
    "drive_write": ("upload file", "write drive", "save to drive"),
    "weather": ("weather", "forecast", "temperature"),
    "apple_notes": ("apple notes", "notes app", "note"),
    "apple_reminders": ("reminder", "reminders app", "todo", "to-do"),
    "things": ("things 3", "things app"),
    "imessage_bridge": ("imessage", "messages app", "sms"),
    "bluebubbles": ("bluebubbles",),
    "fetch_url": ("fetch url", "http request", "web page"),
    "exec": ("shell command", "terminal command", "run command"),
}


@dataclass
class MigrationEntry:
    """A single migration action result."""

    phase: str
    action: str
    status: str
    source: str | None = None
    destination: str | None = None
    detail: str = ""


@dataclass
class MigrationReport:
    """Structured migration report."""

    source_root: str
    target_root: str
    phases: list[str]
    apply: bool
    started_at: str
    finished_at: str = ""
    entries: list[MigrationEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    manual_actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        by_phase: dict[str, int] = {}
        for entry in self.entries:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
            by_phase[entry.phase] = by_phase.get(entry.phase, 0) + 1

        return {
            "source_root": self.source_root,
            "target_root": self.target_root,
            "phases": self.phases,
            "apply": self.apply,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": {
                "entries": len(self.entries),
                "warnings": len(self.warnings),
                "errors": len(self.errors),
                "manual_actions": len(self.manual_actions),
                "by_status": by_status,
                "by_phase": by_phase,
            },
            "entries": [
                {
                    "phase": e.phase,
                    "action": e.action,
                    "status": e.status,
                    "source": e.source,
                    "destination": e.destination,
                    "detail": e.detail,
                }
                for e in self.entries
            ],
            "warnings": self.warnings,
            "errors": self.errors,
            "manual_actions": self.manual_actions,
        }


@dataclass
class OpenClawMigratorOptions:
    """Runtime configuration for OpenClaw migration."""

    source_root: Path
    target_root: Path
    phases: tuple[str, ...] = DEFAULT_PHASES
    apply: bool = False
    overwrite: bool = True
    apply_agent_config: bool = False
    sender_id: str = "openclaw"
    prefer_existing_active_session: bool = True


class OpenClawMigrator:
    """Run OpenClaw -> Creel migration phases."""

    def __init__(self, options: OpenClawMigratorOptions):
        self.options = options
        self.source_root = options.source_root.resolve()
        self.target_root = options.target_root.resolve()
        self.workspace_source = self._detect_workspace_source()

        self.workspace_dir = self.target_root / "workspace"
        self.sessions_dir = self.target_root / "sessions"
        self.tasks_dir = self.target_root / "tasks"
        self.agent_config_path = self.target_root / "agent.yaml"
        self.artifacts_dir = self.target_root / "migrations" / "openclaw"
        timestamp_tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.backup_root = self.target_root / ".migration_backups" / timestamp_tag

        self._agent_overlay: dict[str, Any] = {}
        self._report = MigrationReport(
            source_root=str(self.source_root),
            target_root=str(self.target_root),
            phases=list(options.phases),
            apply=options.apply,
            started_at=datetime.now(UTC).isoformat(),
        )

    def _detect_workspace_source(self) -> Path:
        """Select the most likely source path containing SOUL/USER/AGENTS files."""
        direct = self.source_root
        nested = self.source_root / "workspace"
        required = {"SOUL.md", "USER.md", "AGENTS.md"}

        direct_has = sum((direct / name).exists() for name in required)
        nested_has = sum((nested / name).exists() for name in required) if nested.is_dir() else 0

        if nested_has > direct_has:
            return nested
        return direct

    def run(self) -> MigrationReport:
        """Run configured migration phases."""
        if not self.source_root.is_dir():
            self._error(f"Source directory not found: {self.source_root}")
            self._report.finished_at = datetime.now(UTC).isoformat()
            return self._report

        for phase in self.options.phases:
            if phase == "1":
                self._run_phase1()
            elif phase == "2":
                self._run_phase2()
            elif phase == "3":
                self._run_phase3()
            else:
                self._warn(f"Unknown phase '{phase}' requested; skipping.")

        # Always emit overlay artifacts when we have any mapped agent config.
        self._write_agent_artifacts(phase="meta")
        self._report.finished_at = datetime.now(UTC).isoformat()
        return self._report

    # ---------------------------------------------------------------------
    # Phase 1
    # ---------------------------------------------------------------------

    def _run_phase1(self) -> None:
        phase = "1"
        file_names = ("SOUL.md", "MEMORY.md", "USER.md", "AGENTS.md")
        for name in file_names:
            src = self.workspace_source / name
            dst = self.workspace_dir / name
            if not src.exists():
                self._entry(
                    phase=phase,
                    action=f"import_{name.lower()}",
                    status="missing",
                    source=str(src),
                    destination=str(dst),
                    detail="Source file not found.",
                )
                continue
            content = self._read_text(src, phase, f"read_{name.lower()}")
            if content is None:
                continue
            self._write_text(
                phase=phase,
                action=f"import_{name.lower()}",
                source=src,
                destination=dst,
                content=content,
            )

        # SOUL.md -> persona/system prompt mapping overlay.
        if (self.workspace_source / "SOUL.md").exists():
            self._agent_overlay["system_prompt_file"] = "workspace/SOUL.md"
            self._entry(
                phase=phase,
                action="map_soul_to_system_prompt",
                status="mapped",
                source=str(self.workspace_source / "SOUL.md"),
                destination="agent.yaml (overlay)",
                detail="Mapped via system_prompt_file=workspace/SOUL.md",
            )

        # memory/*.md files.
        source_memory_dir = self.workspace_source / "memory"
        target_memory_dir = self.workspace_dir / "memory"
        if source_memory_dir.is_dir():
            files = sorted(source_memory_dir.glob("*.md"))
            if not files:
                self._entry(
                    phase=phase,
                    action="import_memory_daily",
                    status="skipped",
                    source=str(source_memory_dir),
                    destination=str(target_memory_dir),
                    detail="No memory/*.md files found.",
                )
            for src in files:
                content = self._read_text(src, phase, "read_memory_daily")
                if content is None:
                    continue
                self._write_text(
                    phase=phase,
                    action="import_memory_daily",
                    source=src,
                    destination=target_memory_dir / src.name,
                    content=content,
                )
        else:
            self._entry(
                phase=phase,
                action="import_memory_daily",
                status="missing",
                source=str(source_memory_dir),
                destination=str(target_memory_dir),
                detail="Source memory directory not found.",
            )

        # Conversation history -> sessions.
        self._import_conversations(phase=phase)

    def _import_conversations(self, phase: str) -> None:
        files = self._discover_history_files()
        if not files:
            self._entry(
                phase=phase,
                action="import_conversation_history",
                status="skipped",
                detail="No conversation history files discovered.",
            )
            return

        latest_session_by_sender: dict[str, tuple[str, float]] = {}
        imported_count = 0

        for history_file in files:
            candidates = self._load_conversation_candidates(history_file, phase)
            if not candidates:
                continue

            for idx, candidate in enumerate(candidates):
                normalized = self._normalize_conversation(
                    candidate=candidate,
                    source_path=history_file,
                    ordinal=idx,
                )
                if normalized is None:
                    self._entry(
                        phase=phase,
                        action="import_conversation_history",
                        status="skipped",
                        source=str(history_file),
                        detail=f"Candidate #{idx + 1} could not be normalized.",
                    )
                    continue

                imported_count += 1
                session_id = str(normalized["session_id"])
                destination = self.sessions_dir / f"{session_id}.json"
                payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
                self._write_text(
                    phase=phase,
                    action="import_conversation_history",
                    source=history_file,
                    destination=destination,
                    content=payload,
                )

                sender = str(normalized["sender_id"])
                last_active = float(normalized["last_active"])
                prev = latest_session_by_sender.get(sender)
                if prev is None or last_active > prev[1]:
                    latest_session_by_sender[sender] = (session_id, last_active)

        if imported_count == 0:
            self._entry(
                phase=phase,
                action="import_conversation_history",
                status="skipped",
                detail="Conversation files found but nothing importable.",
            )
            return

        self._update_active_index(latest_session_by_sender, phase)

    def _discover_history_files(self) -> list[Path]:
        paths: list[Path] = []
        for dirname in DEFAULT_HISTORY_DIRS:
            directory = self.source_root / dirname
            if not directory.is_dir():
                continue
            for pattern in ("*.json", "*.jsonl", "*.ndjson"):
                paths.extend(sorted(directory.glob(pattern)))

        for filename in DEFAULT_TOP_LEVEL_HISTORY_FILES:
            path = self.source_root / filename
            if path.is_file():
                paths.append(path)

        # OpenClaw layout commonly stores sessions here.
        agent_sessions_dir = self.source_root / "agents" / "main" / "sessions"
        if agent_sessions_dir.is_dir():
            for pattern in ("*.json", "*.jsonl", "*.ndjson"):
                paths.extend(sorted(agent_sessions_dir.glob(pattern)))

        if not paths:
            for candidate in sorted(self.source_root.rglob("*.json")):
                rel = candidate.relative_to(self.source_root)
                if self._ignore_discovered_relpath(rel):
                    continue
                rel_text = str(rel).lower()
                if any(
                    token in rel_text for token in ("conversation", "session", "history", "chat")
                ):
                    paths.append(candidate)
            for candidate in sorted(self.source_root.rglob("*.jsonl")):
                rel = candidate.relative_to(self.source_root)
                if self._ignore_discovered_relpath(rel):
                    continue
                rel_text = str(rel).lower()
                if any(
                    token in rel_text for token in ("conversation", "session", "history", "chat")
                ):
                    paths.append(candidate)

        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique: list[Path] = []
        for path in paths:
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    @staticmethod
    def _ignore_discovered_relpath(path: Path) -> bool:
        blocked = {".git", ".venv", "node_modules", "__pycache__", "migrations"}
        return any(part in blocked or part.startswith(".") for part in path.parts)

    def _load_conversation_candidates(self, path: Path, phase: str) -> list[Any]:
        raw = self._read_text(path, phase, "read_conversation_file")
        if raw is None:
            return []
        text = raw.strip()
        if not text:
            return []

        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            rows: list[Any] = []
            for lineno, line in enumerate(text.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    self._warn(
                        f"Failed to parse JSONL line {lineno} in {path}. Skipping that line."
                    )
            return self._coerce_payload_to_conversations(rows)

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self._warn(f"Failed to parse JSON in history file: {path}")
            return []
        return self._coerce_payload_to_conversations(payload)

    def _coerce_payload_to_conversations(self, payload: Any) -> list[Any]:
        if isinstance(payload, dict):
            for key in (
                "conversations",
                "sessions",
                "threads",
                "chats",
                "history",
                "data",
            ):
                node = payload.get(key)
                if isinstance(node, list):
                    return node
            messages = self._extract_messages(payload)
            if messages is not None:
                return [payload]
            return []

        if isinstance(payload, list):
            if not payload:
                return []
            if self._looks_like_event_stream(payload):
                return [{"events": payload}]
            if all(self._looks_like_message(item) for item in payload):
                return [{"messages": payload}]
            return payload

        return []

    @staticmethod
    def _looks_like_event_stream(payload: list[Any]) -> bool:
        if not payload:
            return False
        if not all(isinstance(item, dict) for item in payload):
            return False
        types = {str(item.get("type", "")).lower() for item in payload}
        if not types:
            return False
        # OpenClaw JSONL sessions typically contain session/message/model_change/custom events.
        return bool(types.intersection({"session", "message", "model_change", "custom"}))

    @staticmethod
    def _looks_like_message(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        role = item.get("role") or item.get("author") or item.get("speaker") or item.get("from")
        if role is None:
            return False
        if "content" in item:
            return True
        return any(k in item for k in ("text", "message", "body", "output", "input", "value"))

    def _normalize_conversation(
        self,
        *,
        candidate: Any,
        source_path: Path,
        ordinal: int,
    ) -> dict[str, Any] | None:
        messages_raw = self._extract_messages(candidate)
        if not messages_raw:
            return None

        messages: list[dict[str, Any]] = []
        for raw_message in messages_raw:
            normalized = self._normalize_message(raw_message)
            if normalized is not None:
                messages.append(normalized)

        messages = self._trim_to_user_text_start(messages)
        if not messages:
            return None

        sender_id = self._extract_sender_id(candidate) or self.options.sender_id
        created_at = self._parse_timestamp(
            self._pick(candidate, "created_at", "created", "started_at", "timestamp"),
            default=time.time(),
        )
        last_active = self._parse_timestamp(
            self._pick(candidate, "last_active", "updated_at", "updated", "ended_at"),
            default=created_at,
        )
        title = self._extract_title(candidate) or self._derive_title(messages)
        seed = f"{source_path}:{ordinal}:{sender_id}:{created_at:.6f}:{title}"
        session_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

        return {
            "session_id": session_id,
            "sender_id": str(sender_id),
            "title": title,
            "created_at": created_at,
            "last_active": last_active,
            "messages": messages,
            "summary": "",
            "token_count": 0,
        }

    def _extract_messages(self, candidate: Any) -> list[Any] | None:
        if isinstance(candidate, list):
            return candidate
        if not isinstance(candidate, dict):
            return None
        events = candidate.get("events")
        if isinstance(events, list):
            return self._extract_messages_from_event_stream(events)
        for key in ("messages", "history", "turns", "events", "chat"):
            node = candidate.get(key)
            if isinstance(node, list):
                return node
        return None

    def _extract_messages_from_event_stream(self, events: list[Any]) -> list[Any]:
        messages: list[Any] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if str(event.get("type", "")).lower() != "message":
                continue
            node = event.get("message")
            if not isinstance(node, dict):
                continue
            if "timestamp" not in node and event.get("timestamp"):
                node = dict(node)
                node["timestamp"] = event.get("timestamp")
            messages.append(node)
        return messages

    def _extract_sender_id(self, candidate: Any) -> str | None:
        if not isinstance(candidate, dict):
            return None
        for key in (
            "sender_id",
            "user_id",
            "contact",
            "phone",
            "channel_id",
            "participant",
        ):
            value = candidate.get(key)
            if value:
                return str(value)
        return None

    def _extract_title(self, candidate: Any) -> str | None:
        if not isinstance(candidate, dict):
            return None
        for key in ("title", "name", "subject", "thread"):
            value = candidate.get(key)
            if value:
                return str(value).strip()[:120]
        return None

    def _normalize_message(self, raw_message: Any) -> dict[str, Any] | None:
        if isinstance(raw_message, str):
            text = raw_message.strip()
            if not text:
                return None
            return {"role": "user", "content": text}

        if not isinstance(raw_message, dict):
            return None

        role_raw = self._pick(raw_message, "role", "author", "speaker", "from", "type")
        role = self._normalize_role(role_raw)

        content = raw_message.get("content")
        if content is None:
            for key in ("text", "message", "body", "value", "output", "input"):
                if key in raw_message and raw_message.get(key) is not None:
                    content = raw_message.get(key)
                    break
        if content is None:
            return None

        if role in {"assistant", "tool"}:
            blocks = self._normalize_assistant_content(content)
            if not blocks:
                return None
            return {"role": "assistant", "content": blocks}

        if role == "tool_result":
            tool_use_id = (
                raw_message.get("tool_use_id")
                or raw_message.get("toolCallId")
                or raw_message.get("tool_call_id")
                or raw_message.get("toolUseId")
            )
            content_text = ""
            if isinstance(content, list):
                content_text = self._extract_text_from_blocks(content) or self._to_text(content)
            else:
                content_text = self._to_text(content)
            block: dict[str, Any] = {
                "type": "tool_result",
                "content": content_text,
            }
            if tool_use_id:
                block["tool_use_id"] = str(tool_use_id)
            return {"role": "user", "content": [block]}

        if role == "system":
            # Skip OpenClaw system messages when importing session history.
            # Creel session history should begin with user content, and
            # injecting system notes as user messages can change behavior.
            return None

        # Default to user content.
        if self._looks_like_tool_result_blocks(content):
            return {"role": "user", "content": content}

        if isinstance(content, list):
            text = self._extract_text_from_blocks(content) or self._to_text(content)
        else:
            text = self._to_text(content)
        if not text:
            return None
        return {"role": "user", "content": text}

    def _normalize_assistant_content(self, content: Any) -> list[dict[str, Any]]:
        if isinstance(content, list):
            if self._looks_like_blocks(content):
                normalized_blocks: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type", "")).strip()
                    if block_type == "text":
                        text = self._to_text(block.get("text"))
                        if text:
                            normalized_blocks.append({"type": "text", "text": text})
                        continue
                    if block_type == "toolCall":
                        tool_id = block.get("id")
                        tool_name = block.get("name")
                        tool_input = block.get("arguments", {})
                        if tool_id and tool_name:
                            normalized_blocks.append(
                                {
                                    "type": "tool_use",
                                    "id": str(tool_id),
                                    "name": str(tool_name),
                                    "input": (tool_input if isinstance(tool_input, dict) else {}),
                                }
                            )
                        continue
                    if block_type == "thinking":
                        # Preserve prior thinking as plain text instead of Anthropic
                        # thinking blocks to avoid signature/API compatibility issues.
                        thinking = self._to_text(block.get("thinking"))
                        if thinking:
                            normalized_blocks.append(
                                {
                                    "type": "text",
                                    "text": f"[Imported thinking]\n{thinking}",
                                }
                            )
                        continue
                    # Best-effort fallback for unknown assistant block types.
                    fallback = self._to_text(block)
                    if fallback:
                        normalized_blocks.append({"type": "text", "text": fallback})
                return normalized_blocks
            text = self._to_text(content)
            return [{"type": "text", "text": text}] if text else []

        text = self._to_text(content)
        return [{"type": "text", "text": text}] if text else []

    @staticmethod
    def _looks_like_blocks(content: Any) -> bool:
        return isinstance(content, list) and all(
            isinstance(block, dict) and "type" in block for block in content
        )

    @staticmethod
    def _looks_like_tool_result_blocks(content: Any) -> bool:
        if not isinstance(content, list):
            return False
        has_tool_result = False
        for block in content:
            if not isinstance(block, dict):
                return False
            if block.get("type") == "tool_result":
                has_tool_result = True
        return has_tool_result

    @staticmethod
    def _normalize_role(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return "user"
        if text in {"assistant", "ai", "model", "bot"}:
            return "assistant"
        if text in {"user", "human", "client", "person"}:
            return "user"
        if text in {"toolresult", "tool_result"}:
            return "tool_result"
        if text in {"system", "developer"}:
            return "system"
        if text in {"tool", "function"}:
            return "tool"
        if "assistant" in text or "bot" in text or "ai" in text:
            return "assistant"
        if "toolresult" in text:
            return "tool_result"
        if "system" in text:
            return "system"
        if "tool" in text or "function" in text:
            return "tool"
        return "user"

    @staticmethod
    def _extract_text_from_blocks(content: list[Any]) -> str:
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", "")).strip()
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
            elif block_type == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    texts.append(f"[Imported thinking]\n{thinking.strip()}")
        return "\n\n".join(texts).strip()

    @staticmethod
    def _trim_to_user_text_start(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        trimmed = list(messages)
        while trimmed:
            first = trimmed[0]
            if first.get("role") == "user" and isinstance(first.get("content"), str):
                break
            trimmed.pop(0)
        return trimmed

    @staticmethod
    def _derive_title(messages: list[dict[str, Any]]) -> str:
        for message in messages:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()[:60]
        return "Imported OpenClaw Session"

    def _update_active_index(
        self,
        latest_session_by_sender: dict[str, tuple[str, float]],
        phase: str,
    ) -> None:
        if not latest_session_by_sender:
            return

        active_path = self.sessions_dir / "_active.json"
        existing: dict[str, str] = {}
        if active_path.exists():
            raw = self._read_text(active_path, phase, "read_active_index")
            if raw:
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        existing = {str(k): str(v) for k, v in payload.items()}
                except json.JSONDecodeError:
                    self._warn(f"Could not parse existing active index: {active_path}")

        updated = dict(existing)
        for sender, (session_id, _) in latest_session_by_sender.items():
            if sender in updated and self.options.prefer_existing_active_session:
                continue
            updated[sender] = session_id

        content = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
        self._write_text(
            phase=phase,
            action="update_active_session_index",
            source=None,
            destination=active_path,
            content=content,
        )

    # ---------------------------------------------------------------------
    # Phase 2
    # ---------------------------------------------------------------------

    def _run_phase2(self) -> None:
        phase = "2"
        source_config_path, source_config = self._load_openclaw_config(phase)
        if source_config is None:
            self._entry(
                phase=phase,
                action="map_openclaw_config",
                status="skipped",
                detail="No OpenClaw config file discovered.",
            )
            return

        mapped_tools: dict[str, dict[str, Any]] = {}
        mapped_channels: dict[str, dict[str, Any]] = {}

        tools_node = source_config.get("tools")
        for source_name, source_tool in self._iter_named_items(tools_node, "tool"):
            mapped = self._map_tool(source_name, source_tool, phase=phase)
            if mapped is None:
                continue
            key = self._unique_name(source_name, mapped_tools)
            mapped_tools[key] = mapped
            self._entry(
                phase=phase,
                action="map_tool_definition",
                status="mapped",
                source=str(source_config_path),
                destination=f"agent.yaml tools.{key}",
                detail=f"Mapped '{source_name}' -> executor '{mapped['executor']}'",
            )

        integrations_node = (
            source_config.get("integrations")
            or source_config.get("connectors")
            or source_config.get("providers")
        )
        self._map_integrations(
            source=integrations_node,
            config_path=source_config_path,
            mapped_tools=mapped_tools,
            mapped_channels=mapped_channels,
            phase=phase,
        )

        if mapped_tools:
            self._agent_overlay.setdefault("tools", {})
            self._agent_overlay["tools"].update(mapped_tools)

        if mapped_channels:
            self._agent_overlay.setdefault("channels", {})
            self._agent_overlay["channels"].update(mapped_channels)

        self._import_cron_jobs(
            source_config=source_config,
            source_config_path=source_config_path,
            mapped_tools=mapped_tools,
            phase=phase,
        )

    def _load_openclaw_config(self, phase: str) -> tuple[Path | None, dict[str, Any] | None]:
        for rel in DEFAULT_CONFIG_CANDIDATES:
            path = self.source_root / rel
            if not path.exists():
                continue
            content = self._read_text(path, phase, "read_openclaw_config")
            if content is None:
                continue
            try:
                if path.suffix.lower() == ".json":
                    payload = json.loads(content)
                else:
                    payload = yaml.safe_load(content)
            except Exception:
                self._warn(f"Could not parse config file: {path}")
                continue
            if isinstance(payload, dict):
                return path, payload
        return None, None

    def _map_tool(
        self,
        source_name: str,
        source_tool: Any,
        *,
        phase: str,
    ) -> dict[str, Any] | None:
        if not isinstance(source_tool, dict):
            source_tool = {}

        executor = str(source_tool.get("executor") or "").strip()
        if not executor:
            combined = " ".join(
                str(part)
                for part in (
                    source_name,
                    source_tool.get("provider"),
                    source_tool.get("type"),
                    source_tool.get("description"),
                )
                if part
            )
            guesses = self._guess_executors(combined)
            executor = guesses[0] if guesses else ""

        if not executor:
            self._manual(
                f"Tool '{source_name}' has no clear Creel executor mapping. "
                "Define it manually in agent.yaml."
            )
            self._entry(
                phase=phase,
                action="map_tool_definition",
                status="manual",
                detail=f"Tool '{source_name}' could not be mapped.",
            )
            return None

        if not self._executor_exists(executor):
            self._warn(
                f"Mapped tool '{source_name}' -> '{executor}', but that executor "
                "is not present in src/executors."
            )

        description = str(
            source_tool.get("description")
            or source_tool.get("prompt")
            or f"Migrated from OpenClaw tool '{source_name}'"
        )

        mapped: dict[str, Any] = {
            "executor": executor,
            "description": description,
        }

        params = self._normalize_parameters(source_tool.get("parameters"))
        if not params and isinstance(source_tool.get("args"), dict):
            params = {
                str(k): {
                    "type": "string",
                    "description": f"Migrated arg: {k}",
                    "required": False,
                }
                for k in source_tool["args"].keys()
            }
        if params:
            mapped["parameters"] = params

        fixed_args = source_tool.get("fixed_args")
        if isinstance(fixed_args, dict) and fixed_args:
            mapped["fixed_args"] = {str(k): str(v) for k, v in fixed_args.items()}

        secrets = source_tool.get("secrets")
        if isinstance(secrets, str) and secrets.strip():
            mapped["secrets"] = secrets.strip()

        if executor in _NETWORK_EXECUTORS:
            mapped["network"] = True

        return mapped

    def _map_integrations(
        self,
        *,
        source: Any,
        config_path: Path | None,
        mapped_tools: dict[str, dict[str, Any]],
        mapped_channels: dict[str, dict[str, Any]],
        phase: str,
    ) -> None:
        for name, cfg in self._iter_named_items(source, "integration"):
            if not isinstance(cfg, dict):
                cfg = {}

            joined = " ".join(
                str(part).lower()
                for part in (
                    name,
                    cfg.get("type"),
                    cfg.get("provider"),
                    cfg.get("name"),
                )
                if part
            )
            slug = self._safe_slug(name) or "integration"

            if "whatsapp" in joined:
                channel_cfg: dict[str, Any] = {
                    "phone_number": str(cfg.get("phone_number") or cfg.get("phone") or "$PHONE"),
                }
                mode = str(cfg.get("mode") or "").strip()
                if mode in {"polling", "webhook"}:
                    channel_cfg["mode"] = mode
                if isinstance(cfg.get("bridge_url"), str):
                    channel_cfg["bridge_url"] = cfg["bridge_url"]
                if isinstance(cfg.get("poll_interval"), int):
                    channel_cfg["poll_interval"] = cfg["poll_interval"]
                mapped_channels["whatsapp"] = channel_cfg
                self._entry(
                    phase=phase,
                    action="map_integration",
                    status="mapped",
                    source=str(config_path) if config_path else None,
                    destination="agent.yaml channels.whatsapp",
                    detail=f"Mapped integration '{name}' to whatsapp channel.",
                )
                continue

            guesses = self._guess_executors(joined)
            if guesses:
                executor = guesses[0]
                tool_name = self._unique_name(f"{slug}_tool", mapped_tools)
                mapped_tools[tool_name] = {
                    "executor": executor,
                    "description": f"Migrated from OpenClaw integration '{name}'",
                    "parameters": {},
                    "network": executor in _NETWORK_EXECUTORS,
                }
                self._entry(
                    phase=phase,
                    action="map_integration",
                    status="mapped",
                    source=str(config_path) if config_path else None,
                    destination=f"agent.yaml tools.{tool_name}",
                    detail=f"Mapped integration '{name}' -> executor '{executor}'.",
                )
                continue

            self._manual(
                f"Integration '{name}' has no automatic Creel mapping. "
                "Create an executor/tool mapping manually."
            )
            self._entry(
                phase=phase,
                action="map_integration",
                status="manual",
                source=str(config_path) if config_path else None,
                detail=f"Could not map integration '{name}'.",
            )

    def _import_cron_jobs(
        self,
        *,
        source_config: dict[str, Any],
        source_config_path: Path | None,
        mapped_tools: dict[str, dict[str, Any]],
        phase: str,
    ) -> None:
        scheduler_node = source_config.get("scheduler")
        jobs_node = None
        if isinstance(scheduler_node, dict):
            jobs_node = scheduler_node.get("jobs")
        if jobs_node is None:
            jobs_node = (
                source_config.get("cron_jobs")
                or source_config.get("cron")
                or source_config.get("jobs")
                or source_config.get("schedules")
            )

        existing_task_names = {path.stem for path in self.tasks_dir.glob("*.yaml")}

        for source_job_name, job in self._iter_named_items(jobs_node, "job"):
            if not isinstance(job, dict):
                job = {}

            cron = (
                job.get("schedule") or job.get("cron") or job.get("expression") or job.get("rrule")
            )
            normalized_cron = self._normalize_cron(cron)
            if not normalized_cron:
                self._manual(
                    f"Cron job '{source_job_name}' has an unsupported schedule "
                    f"('{cron}'). Convert this task manually."
                )
                self._entry(
                    phase=phase,
                    action="import_cron_job",
                    status="manual",
                    source=str(source_config_path) if source_config_path else None,
                    detail=f"Unsupported cron schedule for '{source_job_name}'.",
                )
                continue

            task_slug = self._safe_slug(f"openclaw_{source_job_name}")
            task_name = task_slug
            i = 2
            while task_name in existing_task_names:
                task_name = f"{task_slug}_{i}"
                i += 1
            existing_task_names.add(task_name)

            executors = self._build_task_executors(job, mapped_tools)
            prompt = str(
                job.get("prompt")
                or job.get("instructions")
                or job.get("description")
                or f"Migrated OpenClaw job '{source_job_name}'."
            ).strip()
            if not prompt:
                prompt = f"Migrated OpenClaw job '{source_job_name}'."

            task_def: dict[str, Any] = {
                "name": task_name,
                "schedule": normalized_cron,
                "executors": executors,
                "prompt": prompt,
                "output": {"type": "stdout", "to": ""},
                "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 300},
            }
            destination = self.tasks_dir / f"{task_name}.yaml"
            content = yaml.safe_dump(task_def, sort_keys=False, allow_unicode=True)
            self._write_text(
                phase=phase,
                action="import_cron_job",
                source=source_config_path,
                destination=destination,
                content=content,
            )

    def _build_task_executors(
        self,
        job: dict[str, Any],
        mapped_tools: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        # If OpenClaw job already has explicit executor map, keep it.
        if isinstance(job.get("executors"), dict):
            result: dict[str, dict[str, Any]] = {}
            for key, value in job["executors"].items():
                if isinstance(value, dict):
                    result[str(key)] = {
                        k: v for k, v in value.items() if k in {"secrets", "args", "timeout"}
                    }
                else:
                    result[str(key)] = {}
            return result

        executor_map: dict[str, dict[str, Any]] = {}
        tools_node = job.get("tools")
        if isinstance(tools_node, list):
            for tool_name in tools_node:
                mapped = mapped_tools.get(str(tool_name))
                if not mapped:
                    continue
                executor_name = str(mapped["executor"])
                executor_map[executor_name] = {}
        return executor_map

    @staticmethod
    def _normalize_cron(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        parts = text.split()
        if len(parts) == 5:
            return text
        # Some schedulers use six fields with seconds first.
        if len(parts) == 6:
            return " ".join(parts[1:])
        return None

    # ---------------------------------------------------------------------
    # Phase 3
    # ---------------------------------------------------------------------

    def _run_phase3(self) -> None:
        phase = "3"
        skill_files = self._discover_skill_files()
        if not skill_files:
            self._entry(
                phase=phase,
                action="migrate_skills",
                status="skipped",
                detail="No SKILL.md files discovered.",
            )
            return

        prompt_skill_sections: list[str] = []
        manual_items: list[str] = []
        skills_artifact_dir = self.artifacts_dir / "skills"

        for skill_file in skill_files:
            content = self._read_text(skill_file, phase, "read_skill_file")
            if content is None:
                continue

            skill_name = skill_file.parent.name
            slug = self._safe_slug(skill_name) or "skill"
            classification, executors = self._classify_skill(skill_name, content)
            rel_source = str(skill_file.relative_to(self.source_root))

            # Keep a copy of the source SKILL.md in workspace/openclaw_skills/.
            copied_skill_path = self.workspace_dir / "openclaw_skills" / f"{slug}.md"
            self._write_text(
                phase=phase,
                action="copy_skill_markdown",
                source=skill_file,
                destination=copied_skill_path,
                content=content,
            )

            metadata = {
                "skill_name": skill_name,
                "source": rel_source,
                "classification": classification,
                "suggested_executors": executors,
            }
            metadata_path = skills_artifact_dir / f"{slug}.yaml"
            self._write_text(
                phase=phase,
                action="write_skill_metadata",
                source=skill_file,
                destination=metadata_path,
                content=yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True),
            )

            if classification == "prompt-only":
                prompt_skill_sections.append(
                    self._format_skill_context(skill_name, rel_source, content)
                )
                self._entry(
                    phase=phase,
                    action="classify_skill",
                    status="mapped",
                    source=str(skill_file),
                    destination="workspace/TOOLS.md",
                    detail=f"'{skill_name}' classified as prompt-only.",
                )
                continue

            if classification == "executor-equivalent" and executors:
                tool_name = self._unique_name(
                    f"skill_{slug}", self._agent_overlay.setdefault("tools", {})
                )
                self._agent_overlay["tools"][tool_name] = {
                    "executor": executors[0],
                    "description": f"Migrated from OpenClaw skill '{skill_name}'",
                    "parameters": {},
                    "network": executors[0] in _NETWORK_EXECUTORS,
                }
                self._entry(
                    phase=phase,
                    action="classify_skill",
                    status="mapped",
                    source=str(skill_file),
                    destination=f"agent.yaml tools.{tool_name}",
                    detail=f"'{skill_name}' mapped to executor '{executors[0]}'.",
                )
                continue

            # Mixed / unsupported.
            recommendation = ", ".join(executors) if executors else "none detected"
            manual_items.append(
                f"- `{skill_name}` ({rel_source}): mixed/unsupported. "
                f"Suggested executors: {recommendation}."
            )
            self._manual(
                f"Skill '{skill_name}' requires manual migration (classification={classification})."
            )
            self._entry(
                phase=phase,
                action="classify_skill",
                status="manual",
                source=str(skill_file),
                detail=f"'{skill_name}' requires manual migration.",
            )

        if prompt_skill_sections:
            section = (
                "## Migrated OpenClaw Skill Context\n\n"
                "These skills were imported as prompt context because they do not map "
                "cleanly to a Creel executor.\n\n" + "\n\n".join(prompt_skill_sections)
            )
            self._upsert_generated_section(
                phase=phase,
                action="inject_prompt_skills_into_tools_md",
                destination=self.workspace_dir / "TOOLS.md",
                begin_marker=SKILLS_BEGIN,
                end_marker=SKILLS_END,
                generated_section=section,
            )

        if manual_items:
            checklist = (
                "# OpenClaw Skills Manual Migration Checklist\n\n"
                "The following skills need manual migration:\n\n" + "\n".join(manual_items) + "\n"
            )
            self._write_text(
                phase=phase,
                action="write_skills_manual_checklist",
                source=None,
                destination=skills_artifact_dir / "MANUAL_CHECKLIST.md",
                content=checklist,
            )

    def _discover_skill_files(self) -> list[Path]:
        files: list[Path] = []
        # Prefer explicit workspace skills if present, then fall back to full source scan.
        roots: list[Path] = []
        ws_skills = self.workspace_source / "skills"
        if ws_skills.is_dir():
            roots.append(ws_skills)
        roots.append(self.source_root)

        seen: set[str] = set()
        for root in roots:
            for path in sorted(root.rglob("SKILL.md")):
                key = str(path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                rel = path.relative_to(self.source_root)
                if self._ignore_discovered_relpath(rel):
                    continue
                # Avoid importing generated migration artifacts.
                if rel.parts and rel.parts[0] == "migrations":
                    continue
                files.append(path)
        return files

    def _classify_skill(self, skill_name: str, content: str) -> tuple[str, list[str]]:
        text = f"{skill_name}\n{content}"
        lower = text.lower()
        executors = self._guess_executors(lower)
        has_code_block = "```" in content
        has_command_markers = any(token in lower for token in ("curl ", "pip ", "npm ", "docker "))

        if executors and not has_code_block and not has_command_markers:
            return "executor-equivalent", executors
        if executors and (has_code_block or has_command_markers):
            return "mixed/unsupported", executors
        if has_code_block or has_command_markers:
            return "mixed/unsupported", executors
        return "prompt-only", executors

    def _format_skill_context(self, skill_name: str, rel_source: str, content: str) -> str:
        max_chars = 1500
        body = content.strip()
        if len(body) > max_chars:
            body = body[:max_chars] + "\n\n[... truncated]"
        return f"### {skill_name}\nSource: `{rel_source}`\n\n{body}"

    def _upsert_generated_section(
        self,
        *,
        phase: str,
        action: str,
        destination: Path,
        begin_marker: str,
        end_marker: str,
        generated_section: str,
    ) -> None:
        existing = ""
        if destination.exists():
            read = self._read_text(destination, phase, "read_generated_section_destination")
            if read is None:
                return
            existing = read

        block = f"{begin_marker}\n{generated_section.strip()}\n{end_marker}\n"
        pattern = re.compile(
            rf"{re.escape(begin_marker)}.*?{re.escape(end_marker)}\n?",
            flags=re.DOTALL,
        )
        if pattern.search(existing):
            updated = pattern.sub(block, existing, count=1)
        else:
            updated = existing.rstrip()
            if updated:
                updated += "\n\n"
            updated += block

        self._write_text(
            phase=phase,
            action=action,
            source=None,
            destination=destination,
            content=updated,
        )

    # ---------------------------------------------------------------------
    # Shared helpers
    # ---------------------------------------------------------------------

    def _write_agent_artifacts(self, phase: str) -> None:
        if not self._agent_overlay:
            return

        overlay_yaml = yaml.safe_dump(self._agent_overlay, sort_keys=False, allow_unicode=True)
        self._write_text(
            phase=phase,
            action="write_agent_overlay",
            source=None,
            destination=self.artifacts_dir / "agent.overlay.yaml",
            content=overlay_yaml,
        )

        base: dict[str, Any] = {}
        if self.agent_config_path.exists():
            raw = self._read_text(self.agent_config_path, phase, "read_agent_config")
            if raw:
                try:
                    payload = yaml.safe_load(raw)
                    if isinstance(payload, dict):
                        base = payload
                except Exception:
                    self._warn(f"Could not parse target agent config: {self.agent_config_path}")

        merged = self._deep_merge_dicts(base, self._agent_overlay)
        merged_yaml = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
        migrated_path = self.artifacts_dir / "agent.migrated.yaml"
        self._write_text(
            phase=phase,
            action="write_agent_migrated",
            source=self.agent_config_path if self.agent_config_path.exists() else None,
            destination=migrated_path,
            content=merged_yaml,
        )

        if self.options.apply and self.options.apply_agent_config:
            self._write_text(
                phase=phase,
                action="apply_agent_config",
                source=migrated_path,
                destination=self.agent_config_path,
                content=merged_yaml,
            )

    @staticmethod
    def _deep_merge_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
        result = dict(base)
        for key, value in overlay.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = OpenClawMigrator._deep_merge_dicts(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _pick(obj: Any, *keys: str) -> Any:
        if not isinstance(obj, dict):
            return None
        for key in keys:
            if key in obj:
                return obj[key]
        return None

    @staticmethod
    def _to_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)

    @staticmethod
    def _parse_timestamp(value: Any, *, default: float) -> float:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000.0
            return ts
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return default
            try:
                num = float(text)
                if num > 10_000_000_000:
                    num /= 1000.0
                return num
            except ValueError:
                pass
            try:
                normalized = text.replace("Z", "+00:00")
                return datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                return default
        return default

    def _normalize_parameters(self, params: Any) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        if isinstance(params, dict):
            for name, value in params.items():
                pname = str(name)
                if isinstance(value, dict):
                    normalized[pname] = {
                        "type": str(value.get("type") or "string"),
                        "description": str(value.get("description") or ""),
                        "required": bool(value.get("required", False)),
                    }
                else:
                    normalized[pname] = {
                        "type": "string",
                        "description": f"Migrated parameter: {pname}",
                        "required": False,
                    }
        elif isinstance(params, list):
            for value in params:
                pname = str(value)
                normalized[pname] = {
                    "type": "string",
                    "description": f"Migrated parameter: {pname}",
                    "required": False,
                }
        return normalized

    @staticmethod
    def _iter_named_items(node: Any, default_prefix: str) -> list[tuple[str, Any]]:
        items: list[tuple[str, Any]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                items.append((str(key), value))
            return items
        if isinstance(node, list):
            for idx, value in enumerate(node, start=1):
                if isinstance(value, dict):
                    name = (
                        value.get("name")
                        or value.get("id")
                        or value.get("key")
                        or f"{default_prefix}_{idx}"
                    )
                    items.append((str(name), value))
                else:
                    items.append((f"{default_prefix}_{idx}", value))
        return items

    def _guess_executors(self, text: str) -> list[str]:
        lowered = text.lower()
        matches: list[str] = []
        for executor, keywords in _EXECUTOR_KEYWORDS.items():
            if any(keyword in lowered for keyword in keywords):
                matches.append(executor)
        return matches

    def _executor_exists(self, executor_name: str) -> bool:
        return (self.target_root / "src" / "executors" / executor_name).is_dir()

    @staticmethod
    def _safe_slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
        slug = re.sub(r"_+", "_", slug).strip("_")
        return slug or "item"

    @staticmethod
    def _unique_name(name: str, existing: dict[str, Any]) -> str:
        base = OpenClawMigrator._safe_slug(name)
        if base not in existing:
            return base
        i = 2
        while f"{base}_{i}" in existing:
            i += 1
        return f"{base}_{i}"

    def _read_text(self, path: Path, phase: str, action: str) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            self._entry(
                phase=phase,
                action=action,
                status="error",
                source=str(path),
                detail=f"Failed to read file: {exc}",
            )
            self._error(f"Failed reading {path}: {exc}")
            return None

    def _write_text(
        self,
        *,
        phase: str,
        action: str,
        source: Path | None,
        destination: Path,
        content: str,
    ) -> Path:
        src = str(source) if source else None
        dst = str(destination)
        if not self.options.apply:
            self._entry(
                phase=phase,
                action=action,
                status="planned",
                source=src,
                destination=dst,
            )
            return destination

        target = destination
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            existing = self._read_text(target, phase, f"{action}_compare_existing")
            if existing is not None and existing == content:
                self._entry(
                    phase=phase,
                    action=action,
                    status="unchanged",
                    source=src,
                    destination=dst,
                )
                return target
            if self.options.overwrite:
                self._backup_existing(target, phase)
            else:
                target = self._conflict_path(target)
                target.parent.mkdir(parents=True, exist_ok=True)

        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            self._entry(
                phase=phase,
                action=action,
                status="error",
                source=src,
                destination=dst,
                detail=f"Failed to write file: {exc}",
            )
            self._error(f"Failed writing {target}: {exc}")
            return target

        status = "written" if target == destination else "written_conflict"
        detail = ""
        if target != destination:
            detail = f"Destination existed and overwrite=false; wrote to {target}."
        self._entry(
            phase=phase,
            action=action,
            status=status,
            source=src,
            destination=str(target),
            detail=detail,
        )
        return target

    def _backup_existing(self, path: Path, phase: str) -> None:
        try:
            rel = path.relative_to(self.target_root)
        except ValueError:
            rel = Path(path.name)
        backup_path = self.backup_root / rel
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            backup_path.write_bytes(path.read_bytes())
            self._entry(
                phase=phase,
                action="backup_existing_file",
                status="written",
                source=str(path),
                destination=str(backup_path),
                detail="Backed up overwritten file.",
            )
        except OSError as exc:
            self._warn(f"Failed to create backup for {path}: {exc}")

    @staticmethod
    def _conflict_path(path: Path) -> Path:
        if path.suffix:
            candidate = path.with_name(f"{path.stem}.openclaw-import{path.suffix}")
        else:
            candidate = path.with_name(f"{path.name}.openclaw-import")
        i = 2
        result = candidate
        while result.exists():
            if path.suffix:
                result = path.with_name(f"{path.stem}.openclaw-import-{i}{path.suffix}")
            else:
                result = path.with_name(f"{path.name}.openclaw-import-{i}")
            i += 1
        return result

    def _entry(
        self,
        *,
        phase: str,
        action: str,
        status: str,
        source: str | None = None,
        destination: str | None = None,
        detail: str = "",
    ) -> None:
        self._report.entries.append(
            MigrationEntry(
                phase=phase,
                action=action,
                status=status,
                source=source,
                destination=destination,
                detail=detail,
            )
        )

    def _warn(self, text: str) -> None:
        self._report.warnings.append(text)

    def _error(self, text: str) -> None:
        self._report.errors.append(text)

    def _manual(self, text: str) -> None:
        self._report.manual_actions.append(text)


def _parse_phases(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_PHASES
    seen: list[str] = []
    for token in raw.split(","):
        phase = token.strip()
        if not phase:
            continue
        if phase not in seen:
            seen.append(phase)
    return tuple(seen) if seen else DEFAULT_PHASES


def _format_terminal_summary(report: MigrationReport) -> str:
    data = report.as_dict()
    summary = data["summary"]
    lines = [
        f"OpenClaw migration {'apply' if report.apply else 'dry-run'} complete.",
        f"Phases: {', '.join(report.phases)}",
        f"Entries: {summary['entries']}",
        f"Warnings: {summary['warnings']}",
        f"Errors: {summary['errors']}",
        f"Manual actions: {summary['manual_actions']}",
    ]
    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in report.warnings[:10]:
            lines.append(f"- {warning}")
        if len(report.warnings) > 10:
            lines.append(f"- ... and {len(report.warnings) - 10} more")
    if report.manual_actions:
        lines.append("")
        lines.append("Manual actions:")
        for item in report.manual_actions[:10]:
            lines.append(f"- {item}")
        if len(report.manual_actions) > 10:
            lines.append(f"- ... and {len(report.manual_actions) - 10} more")
    return "\n".join(lines)


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate-openclaw",
        description="Migrate OpenClaw workspace/config/history into Creel.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to OpenClaw source directory.",
    )
    parser.add_argument(
        "--target-root",
        type=Path,
        default=Path("."),
        help="Path to Creel repo root (default: current directory).",
    )
    parser.add_argument(
        "--phases",
        type=str,
        default="1,2",
        help="Comma-separated phases to run (default: 1,2).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to the target repo. Without this, runs as dry-run.",
    )
    parser.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Overwrite conflicting files (default: true).",
    )
    parser.add_argument(
        "--apply-agent-config",
        action="store_true",
        help="Also write merged agent.migrated.yaml directly to agent.yaml.",
    )
    parser.add_argument(
        "--sender-id",
        type=str,
        default="openclaw",
        help="Fallback sender ID for imported sessions (default: openclaw).",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        default=None,
        help="Optional output path for JSON migration report.",
    )
    parser.add_argument(
        "--prefer-existing-active-session",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep existing sessions/_active.json sender mappings by default.",
    )

    args = parser.parse_args(argv)
    options = OpenClawMigratorOptions(
        source_root=args.source,
        target_root=args.target_root,
        phases=_parse_phases(args.phases),
        apply=bool(args.apply),
        overwrite=bool(args.overwrite),
        apply_agent_config=bool(args.apply_agent_config),
        sender_id=args.sender_id,
        prefer_existing_active_session=bool(args.prefer_existing_active_session),
    )
    migrator = OpenClawMigrator(options)
    report = migrator.run()

    print(_format_terminal_summary(report))

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nReport written to: {args.report_json}")

    if report.errors:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
