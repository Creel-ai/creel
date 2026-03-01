"""Tests for the OpenClaw migration scripts."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from creel.migrations.openclaw import OpenClawMigrator, OpenClawMigratorOptions


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_phase1_imports_workspace_memory_and_history(tmp_path: Path) -> None:
    source = tmp_path / "openclaw"
    target = tmp_path / "creel"
    source.mkdir(parents=True)
    target.mkdir(parents=True)

    _write(source / "SOUL.md", "# Soul\nBe precise.\n")
    _write(source / "MEMORY.md", "# Long-Term Memory\n- likes tea\n")
    _write(source / "USER.md", "# User\n- timezone: America/New_York\n")
    _write(source / "AGENTS.md", "# Agents\nFollow instructions.\n")
    _write(source / "memory" / "2026-02-01.md", "# Memory - 2026-02-01\n- [10:00] note\n")

    history_payload = {
        "sender_id": "alice",
        "title": "Imported thread",
        "messages": [
            {"role": "system", "content": "internal policy"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
    }
    _write(
        source / "sessions" / "conversation.json",
        json.dumps(history_payload, indent=2),
    )

    options = OpenClawMigratorOptions(
        source_root=source,
        target_root=target,
        phases=("1",),
        apply=True,
    )
    report = OpenClawMigrator(options).run()

    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "# Soul\nBe precise.\n"
    assert (target / "workspace" / "MEMORY.md").exists()
    assert (target / "workspace" / "USER.md").exists()
    assert (target / "workspace" / "AGENTS.md").exists()
    assert (target / "workspace" / "memory" / "2026-02-01.md").exists()

    session_files = [p for p in (target / "sessions").glob("*.json") if p.name != "_active.json"]
    assert len(session_files) == 1
    session_payload = json.loads(session_files[0].read_text(encoding="utf-8"))
    assert session_payload["sender_id"] == "alice"
    assert session_payload["messages"][0] == {"role": "user", "content": "Hello"}
    assert session_payload["messages"][1]["role"] == "assistant"
    assert session_payload["messages"][1]["content"][0]["type"] == "text"

    active = json.loads((target / "sessions" / "_active.json").read_text(encoding="utf-8"))
    assert active["alice"] == session_payload["session_id"]
    assert report.errors == []


def test_phase2_maps_tools_integrations_and_cron(tmp_path: Path) -> None:
    source = tmp_path / "openclaw"
    target = tmp_path / "creel"
    source.mkdir(parents=True)
    target.mkdir(parents=True)

    _write(target / "agent.yaml", "system_prompt: test\ntools: {}\n")

    _write(
        source / "openclaw.yaml",
        """
tools:
  web_lookup:
    provider: brave
    description: "Search the web"
    parameters:
      query:
        type: string
        description: Search query
        required: true
integrations:
  whatsapp:
    phone_number: "+15551234567"
cron_jobs:
  - name: morning_digest
    schedule: "0 7 * * *"
    prompt: "Send my digest"
    tools:
      - web_lookup
""".strip()
        + "\n",
    )

    options = OpenClawMigratorOptions(
        source_root=source,
        target_root=target,
        phases=("2",),
        apply=True,
    )
    report = OpenClawMigrator(options).run()

    overlay_path = target / "migrations" / "openclaw" / "agent.overlay.yaml"
    overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    assert overlay["tools"]["web_lookup"]["executor"] == "brave_search"
    assert overlay["channels"]["whatsapp"]["phone_number"] == "+15551234567"

    task_path = target / "tasks" / "openclaw_morning_digest.yaml"
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    assert task["schedule"] == "0 7 * * *"
    assert "brave_search" in task["executors"]
    assert task["output"]["type"] == "stdout"

    assert report.errors == []


def test_phase3_builds_skill_hybrid_outputs(tmp_path: Path) -> None:
    source = tmp_path / "openclaw"
    target = tmp_path / "creel"
    source.mkdir(parents=True)
    target.mkdir(parents=True)

    _write(source / "skills" / "persona" / "SKILL.md", "# Persona Skill\nKeep concise answers.\n")
    _write(
        source / "skills" / "websearch" / "SKILL.md",
        "# Web Search\nUse Brave web search for research.\n",
    )
    _write(
        source / "skills" / "gmail_ops" / "SKILL.md",
        "# Gmail Ops\nUse Gmail and shell scripts.\n\n```bash\ncurl https://example.com\n```\n",
    )

    options = OpenClawMigratorOptions(
        source_root=source,
        target_root=target,
        phases=("3",),
        apply=True,
    )
    report = OpenClawMigrator(options).run()

    tools_md = (target / "workspace" / "TOOLS.md").read_text(encoding="utf-8")
    assert "OPENCLAW_SKILLS_BEGIN" in tools_md
    assert "Persona Skill" in tools_md

    assert (target / "workspace" / "openclaw_skills" / "persona.md").exists()
    assert (target / "workspace" / "openclaw_skills" / "websearch.md").exists()

    checklist_path = target / "migrations" / "openclaw" / "skills" / "MANUAL_CHECKLIST.md"
    checklist = checklist_path.read_text(encoding="utf-8")
    assert "gmail_ops" in checklist

    overlay = yaml.safe_load(
        (target / "migrations" / "openclaw" / "agent.overlay.yaml").read_text(encoding="utf-8")
    )
    tool_defs = overlay.get("tools", {})
    assert any(tool.get("executor") == "brave_search" for tool in tool_defs.values())

    assert report.errors == []
