"""Slash command registry — extensible command system for chat and TUI."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ChatContext:
    """Context passed to slash command handlers."""

    sender_id: str
    server: Any  # ChatServer instance


@dataclass
class SlashCommand:
    """A registered slash command."""

    name: str
    description: str
    handler: Callable[[str, ChatContext], str]  # (args_text, context) -> response
    aliases: list[str] = field(default_factory=list)
    hidden: bool = False
    category: str = "General"


class SlashCommandRegistry:
    """Registry for slash commands with routing, help, and alias support."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}  # alias -> canonical name

    def register(self, cmd: SlashCommand) -> None:
        """Register a slash command. Raises ValueError on name collision."""
        name = cmd.name.lower()
        if name in self._commands or name in self._aliases:
            raise ValueError(f"Command /{name} is already registered")
        self._commands[name] = cmd
        for alias in cmd.aliases:
            alias_lower = alias.lower()
            if alias_lower in self._commands or alias_lower in self._aliases:
                raise ValueError(f"Alias /{alias_lower} conflicts with an existing command")
            self._aliases[alias_lower] = name

    def handle(self, text: str, context: ChatContext) -> str | None:
        """Try to handle a slash command. Returns response or None if not a command."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None

        parts = stripped.split(maxsplit=1)
        cmd_name = parts[0][1:].lower()  # strip leading /
        args_text = parts[1] if len(parts) > 1 else ""

        # Resolve alias
        canonical = self._aliases.get(cmd_name, cmd_name)

        cmd = self._commands.get(canonical)
        if cmd is None:
            return self._unknown_command(cmd_name)

        try:
            return cmd.handler(args_text, context)
        except Exception:
            logger.exception("Error handling /%s", cmd_name)
            return f"Error executing /{cmd_name}. Check logs for details."

    def is_command(self, text: str) -> bool:
        """Check if text is a registered slash command (for routing)."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return False
        cmd_name = stripped.split()[0][1:].lower()
        canonical = self._aliases.get(cmd_name, cmd_name)
        return canonical in self._commands

    def get_command(self, name: str) -> SlashCommand | None:
        """Look up a command by name or alias."""
        name = name.lower().lstrip("/")
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def help_text(self, *, rich: bool = False) -> str:
        """Generate help text from all registered (non-hidden) commands."""
        visible = [c for c in self._commands.values() if not c.hidden]

        # Group by category
        categories: dict[str, list[SlashCommand]] = {}
        for cmd in visible:
            categories.setdefault(cmd.category, []).append(cmd)

        lines: list[str] = []
        for cat, cmds in categories.items():
            if rich:
                lines.append(f"[bold]{cat}:[/bold]")
            else:
                lines.append(f"{cat}:")
            for cmd in sorted(cmds, key=lambda c: c.name):
                name = f"/{cmd.name}"
                if rich:
                    lines.append(f"  [cyan]{name:<18s}[/cyan] {cmd.description}")
                else:
                    lines.append(f"  {name:<18s} {cmd.description}")
            lines.append("")

        return "\n".join(lines).rstrip()

    def command_detail(self, name: str) -> str:
        """Return detailed help for a specific command."""
        cmd = self.get_command(name)
        if cmd is None:
            return f"Unknown command: /{name}"
        lines = [f"/{cmd.name} — {cmd.description}"]
        if cmd.aliases:
            lines.append(f"  Aliases: {', '.join('/' + a for a in cmd.aliases)}")
        lines.append(f"  Category: {cmd.category}")
        return "\n".join(lines)

    def telegram_bot_commands(self) -> list[dict]:
        """Return command list suitable for Telegram setMyCommands API."""
        visible = [c for c in self._commands.values() if not c.hidden]
        return [
            {"command": cmd.name, "description": cmd.description[:256]}
            for cmd in sorted(visible, key=lambda c: c.name)
        ]

    def all_command_names(self) -> list[str]:
        """Return all registered command names (without /)."""
        return sorted(self._commands.keys())

    def _unknown_command(self, cmd_name: str) -> str:
        """Generate an unknown command message with suggestions."""
        all_names = self.all_command_names()

        # Simple prefix matching for suggestions
        suggestions = [n for n in all_names if n.startswith(cmd_name[:2])]
        if not suggestions:
            suggestions = all_names[:5]

        msg = f"Unknown command: /{cmd_name}"
        if suggestions:
            suggestion_str = ", ".join(f"/{s}" for s in suggestions[:3])
            msg += f"\nDid you mean: {suggestion_str}?"
        msg += "\nType /help to see available commands."
        return msg


# ---------------------------------------------------------------------------
# Built-in command handlers
# ---------------------------------------------------------------------------


def _cmd_help(args: str, ctx: ChatContext) -> str:
    """Handle /help [command]."""
    registry: SlashCommandRegistry = ctx.server._command_registry
    if args.strip():
        return registry.command_detail(args.strip())
    return registry.help_text()


def _cmd_new(args: str, ctx: ChatContext) -> str:
    session = ctx.server._session_mgr.new_session(ctx.sender_id)
    ctx.server._session_states.pop(ctx.sender_id, None)
    return f"Started new session {session.session_id}."


def _cmd_sessions(args: str, ctx: ChatContext) -> str:
    return ctx.server._format_sessions_list(ctx.sender_id)


def _cmd_status(args: str, ctx: ChatContext) -> str:
    return ctx.server._format_status(ctx.sender_id)


def _cmd_model(args: str, ctx: ChatContext) -> str:
    return ctx.server._format_model()


def _cmd_compact(args: str, ctx: ChatContext) -> str:
    return ctx.server._handle_compact(ctx.sender_id)


def _cmd_resume(args: str, ctx: ChatContext) -> str:
    if not args.strip():
        return "Usage: /resume <session_id>"
    return ctx.server._handle_resume(ctx.sender_id, f"/resume {args}")


def _cmd_clear(args: str, ctx: ChatContext) -> str:
    ctx.server._session_mgr.clear(ctx.sender_id)
    ctx.server._session_states.pop(ctx.sender_id, None)
    return "Session cleared."


def _cmd_allow(args: str, ctx: ChatContext) -> str:
    return ctx.server._handle_allow(ctx.sender_id, f"/allow {args}")


def _cmd_deny(args: str, ctx: ChatContext) -> str:
    return ctx.server._handle_deny(ctx.sender_id, f"/deny {args}")


def _cmd_allows(args: str, ctx: ChatContext) -> str:
    return ctx.server._handle_allows(ctx.sender_id)


def _cmd_tools(args: str, ctx: ChatContext) -> str:
    """List available tools and their status."""
    tools_config = ctx.server._agent_def.tools
    if not tools_config:
        return "No tools configured."

    lines = ["Available tools:", ""]
    for name, cfg in sorted(tools_config.items()):
        executor = cfg.executor
        network = "net" if cfg.network else "   "
        lines.append(f"  {name:<30s}  [{executor}]  {network}")

    lines.append(f"\n{len(tools_config)} tools loaded.")
    return "\n".join(lines)


def _cmd_usage(args: str, ctx: ChatContext) -> str:
    """Show token usage for the current session."""
    session = ctx.server._session_mgr.get_or_create(ctx.sender_id)
    token_count = getattr(session, "token_count", 0) or 0
    msg_count = len(session.messages) if session.messages else 0
    lines = [
        "Usage:",
        f"  Session: {session.session_id}",
        f"  Messages: {msg_count}",
        f"  Tokens (last input): {token_count}",
    ]
    return "\n".join(lines)


def _cmd_audit(args: str, ctx: ChatContext) -> str:
    """Show recent guardian audit entries."""
    if not ctx.server._guardian:
        return "Guardian is not enabled."

    from guardian.audit import read_audit_log

    agent_def = ctx.server._agent_def
    audit_cfg = agent_def.guardian.audit if agent_def.guardian else None
    if not audit_cfg:
        return "Audit logging is not configured."

    from creel import paths

    log_file = audit_cfg.log_file or str(paths.audit_log())

    # Parse count from args (default 10)
    count = 10
    if args.strip().isdigit():
        count = int(args.strip())
    count = min(count, 50)

    entries = read_audit_log(log_file, tail=count)
    if not entries:
        return "No audit entries found."

    lines = [f"Last {len(entries)} audit entries:", ""]
    for entry in entries:
        event = entry.get("event", "?")
        ts = entry.get("ts", "?")
        if len(ts) > 19:
            ts = ts[:19]
        tool = entry.get("tool_name", "")
        verdict = entry.get("verdict", "")
        blocked = entry.get("blocked", "")

        detail = tool
        if verdict:
            detail += f" [{verdict}]"
        if blocked:
            detail += " BLOCKED"
        lines.append(f"  {ts}  {event:<22s}  {detail}")

    return "\n".join(lines)


def _cmd_export(args: str, ctx: ChatContext) -> str:
    """Export current session transcript as text."""
    session = ctx.server._session_mgr.get_or_create(ctx.sender_id)
    if not session.messages:
        return "No messages in current session."

    lines = [f"# Session {session.session_id}", ""]
    for msg in session.messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        text_parts.append(f"[tool_use: {block.get('name', '?')}]")
                    elif block.get("type") == "tool_result":
                        result_content = block.get("content", "")
                        preview = result_content[:100] if isinstance(result_content, str) else "..."
                        text_parts.append(f"[tool_result: {preview}]")
            text = "\n".join(text_parts)
        else:
            text = str(content)

        lines.append(f"**{role}**: {text}")
        lines.append("")

    return "\n".join(lines)


def _cmd_debug(args: str, ctx: ChatContext) -> str:
    """Toggle debug mode."""
    agent_def = ctx.server._agent_def
    if agent_def.guardian:
        current = agent_def.guardian.debug
        agent_def.guardian.debug = not current
        state = "ON" if not current else "OFF"
        # Audit log the toggle so security state changes are tracked
        guardian = ctx.server._guardian
        if guardian and hasattr(guardian, "_audit") and guardian._audit:
            guardian._audit._write(
                {
                    "event": "debug_toggled",
                    "state": state.lower(),
                    "toggled_by": ctx.sender_id,
                }
            )
        return f"Debug mode: {state}"
    return "No guardian config to toggle debug on."


def _cmd_context(args: str, ctx: ChatContext) -> str:
    """Show what context is being injected into the system prompt."""
    ws_cfg = ctx.server._agent_def.workspace
    lines = ["Context injection:", ""]
    lines.append(f"  Workspace: {ws_cfg.path}")
    lines.append(f"  Memory mode: {ws_cfg.memory_context_mode}")
    lines.append(f"  Memory days: {ws_cfg.memory_days}")
    lines.append(f"  Max chars/file: {ws_cfg.max_chars_per_file}")

    # Count workspace files
    from pathlib import Path

    ws_path = Path(ws_cfg.path)
    if ws_path.is_dir():
        md_files = list(ws_path.glob("*.md"))
        lines.append(f"  Workspace files: {len(md_files)}")
        for f in sorted(md_files)[:10]:
            lines.append(f"    - {f.name}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry builder — creates a registry with all built-in commands
# ---------------------------------------------------------------------------


def build_default_registry() -> SlashCommandRegistry:
    """Create a registry pre-populated with all built-in slash commands."""
    registry = SlashCommandRegistry()

    # Session & Status
    registry.register(
        SlashCommand(
            name="help",
            description="Show available commands",
            handler=_cmd_help,
            category="Help",
        )
    )
    registry.register(
        SlashCommand(
            name="new",
            description="Start a new session",
            handler=_cmd_new,
            category="Session",
        )
    )
    registry.register(
        SlashCommand(
            name="sessions",
            description="List recent sessions",
            handler=_cmd_sessions,
            category="Session",
        )
    )
    registry.register(
        SlashCommand(
            name="resume",
            description="Resume a session by ID",
            handler=_cmd_resume,
            category="Session",
        )
    )
    registry.register(
        SlashCommand(
            name="status",
            description="Show server status info",
            handler=_cmd_status,
            category="Status",
        )
    )
    registry.register(
        SlashCommand(
            name="model",
            description="Show current model config",
            handler=_cmd_model,
            category="Status",
        )
    )
    registry.register(
        SlashCommand(
            name="usage",
            description="Show token usage for current session",
            handler=_cmd_usage,
            category="Status",
        )
    )
    registry.register(
        SlashCommand(
            name="compact",
            description="Compact memory/context",
            handler=_cmd_compact,
            category="Session",
        )
    )
    registry.register(
        SlashCommand(
            name="clear",
            description="Clear session history",
            handler=_cmd_clear,
            aliases=["reset"],
            category="Session",
        )
    )
    registry.register(
        SlashCommand(
            name="context",
            description="Show injected context (memory, workspace)",
            handler=_cmd_context,
            category="Session",
        )
    )

    # Tools & Guardian
    registry.register(
        SlashCommand(
            name="tools",
            description="List available tools",
            handler=_cmd_tools,
            category="Tools & Guardian",
        )
    )
    registry.register(
        SlashCommand(
            name="allow",
            description="Temporarily allow a tool pattern",
            handler=_cmd_allow,
            category="Tools & Guardian",
        )
    )
    registry.register(
        SlashCommand(
            name="deny",
            description="Revoke a temporary override",
            handler=_cmd_deny,
            category="Tools & Guardian",
        )
    )
    registry.register(
        SlashCommand(
            name="allows",
            description="List active overrides",
            handler=_cmd_allows,
            category="Tools & Guardian",
        )
    )
    registry.register(
        SlashCommand(
            name="audit",
            description="Show recent guardian audit entries",
            handler=_cmd_audit,
            category="Tools & Guardian",
        )
    )

    # Export & Debug
    registry.register(
        SlashCommand(
            name="export",
            description="Export current session transcript",
            handler=_cmd_export,
            category="Export & Debug",
        )
    )
    registry.register(
        SlashCommand(
            name="debug",
            description="Toggle debug mode",
            handler=_cmd_debug,
            category="Export & Debug",
        )
    )

    return registry
