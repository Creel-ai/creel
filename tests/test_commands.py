"""Tests for the slash command registry (creel.commands)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creel.commands import (
    ChatContext,
    SlashCommand,
    SlashCommandRegistry,
    build_default_registry,
)

# --- SlashCommandRegistry ---


class TestSlashCommandRegistry:
    @pytest.fixture
    def registry(self):
        return SlashCommandRegistry()

    def test_register_and_handle(self, registry):
        registry.register(
            SlashCommand(
                name="ping",
                description="Ping test",
                handler=lambda args, ctx: "pong",
            )
        )
        ctx = ChatContext(sender_id="test", server=MagicMock())
        result = registry.handle("/ping", ctx)
        assert result == "pong"

    def test_handle_with_args(self, registry):
        registry.register(
            SlashCommand(
                name="echo",
                description="Echo text",
                handler=lambda args, ctx: f"echo: {args}",
            )
        )
        ctx = ChatContext(sender_id="test", server=MagicMock())
        result = registry.handle("/echo hello world", ctx)
        assert result == "echo: hello world"

    def test_handle_unknown_command(self, registry):
        registry.register(
            SlashCommand(
                name="help",
                description="Help",
                handler=lambda args, ctx: "help text",
            )
        )
        ctx = ChatContext(sender_id="test", server=MagicMock())
        result = registry.handle("/nonexistent", ctx)
        assert result is not None
        assert "Unknown command" in result
        assert "/help" in result

    def test_handle_non_command(self, registry):
        ctx = ChatContext(sender_id="test", server=MagicMock())
        result = registry.handle("hello", ctx)
        assert result is None

    def test_alias(self, registry):
        registry.register(
            SlashCommand(
                name="clear",
                description="Clear",
                handler=lambda args, ctx: "cleared",
                aliases=["reset"],
            )
        )
        ctx = ChatContext(sender_id="test", server=MagicMock())
        assert registry.handle("/clear", ctx) == "cleared"
        assert registry.handle("/reset", ctx) == "cleared"

    def test_duplicate_name_raises(self, registry):
        registry.register(SlashCommand(name="foo", description="Foo", handler=lambda a, c: ""))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(
                SlashCommand(name="foo", description="Foo 2", handler=lambda a, c: "")
            )

    def test_duplicate_alias_raises(self, registry):
        registry.register(
            SlashCommand(
                name="foo",
                description="Foo",
                handler=lambda a, c: "",
                aliases=["bar"],
            )
        )
        with pytest.raises(ValueError, match="conflicts"):
            registry.register(
                SlashCommand(
                    name="baz",
                    description="Baz",
                    handler=lambda a, c: "",
                    aliases=["bar"],
                )
            )

    def test_is_command(self, registry):
        registry.register(SlashCommand(name="ping", description="Ping", handler=lambda a, c: ""))
        assert registry.is_command("/ping")
        assert registry.is_command("/ping some args")
        assert not registry.is_command("/nonexistent")
        assert not registry.is_command("hello")

    def test_help_text(self, registry):
        registry.register(
            SlashCommand(
                name="status",
                description="Show status",
                handler=lambda a, c: "",
                category="Info",
            )
        )
        registry.register(
            SlashCommand(
                name="new",
                description="Start a new session",
                handler=lambda a, c: "",
                category="Session",
            )
        )
        text = registry.help_text()
        assert "/status" in text
        assert "/new" in text
        assert "Info:" in text
        assert "Session:" in text

    def test_hidden_command_excluded_from_help(self, registry):
        registry.register(
            SlashCommand(
                name="secret",
                description="Hidden",
                handler=lambda a, c: "",
                hidden=True,
            )
        )
        text = registry.help_text()
        assert "/secret" not in text

    def test_command_detail(self, registry):
        registry.register(
            SlashCommand(
                name="status",
                description="Show status info",
                handler=lambda a, c: "",
                aliases=["info"],
                category="Info",
            )
        )
        detail = registry.command_detail("status")
        assert "/status" in detail
        assert "Show status info" in detail
        assert "/info" in detail

    def test_command_detail_unknown(self, registry):
        detail = registry.command_detail("nonexistent")
        assert "Unknown" in detail

    def test_telegram_bot_commands(self, registry):
        registry.register(SlashCommand(name="help", description="Help", handler=lambda a, c: ""))
        registry.register(
            SlashCommand(
                name="secret",
                description="Hidden",
                handler=lambda a, c: "",
                hidden=True,
            )
        )
        cmds = registry.telegram_bot_commands()
        assert len(cmds) == 1
        assert cmds[0]["command"] == "help"
        assert cmds[0]["description"] == "Help"

    def test_all_command_names(self, registry):
        registry.register(SlashCommand(name="b", description="B", handler=lambda a, c: ""))
        registry.register(SlashCommand(name="a", description="A", handler=lambda a, c: ""))
        assert registry.all_command_names() == ["a", "b"]

    def test_case_insensitive(self, registry):
        registry.register(
            SlashCommand(
                name="Ping",
                description="Ping",
                handler=lambda a, c: "pong",
            )
        )
        ctx = ChatContext(sender_id="test", server=MagicMock())
        assert registry.handle("/PING", ctx) == "pong"
        assert registry.handle("/ping", ctx) == "pong"

    def test_handler_exception(self, registry):
        def bad_handler(args, ctx):
            raise RuntimeError("boom")

        registry.register(SlashCommand(name="boom", description="Boom", handler=bad_handler))
        ctx = ChatContext(sender_id="test", server=MagicMock())
        result = registry.handle("/boom", ctx)
        assert "Error" in result

    def test_get_command(self, registry):
        registry.register(
            SlashCommand(
                name="ping",
                description="Ping",
                handler=lambda a, c: "",
                aliases=["p"],
            )
        )
        assert registry.get_command("ping") is not None
        assert registry.get_command("p") is not None
        assert registry.get_command("/ping") is not None
        assert registry.get_command("nonexistent") is None

    def test_unknown_command_suggestions(self, registry):
        registry.register(
            SlashCommand(name="status", description="Status", handler=lambda a, c: "")
        )
        registry.register(
            SlashCommand(name="sessions", description="Sessions", handler=lambda a, c: "")
        )
        ctx = ChatContext(sender_id="test", server=MagicMock())
        result = registry.handle("/sta", ctx)
        assert "Did you mean" in result


# --- build_default_registry ---


class TestDefaultRegistry:
    def test_all_expected_commands_registered(self):
        registry = build_default_registry()
        expected = [
            "help",
            "new",
            "sessions",
            "resume",
            "status",
            "model",
            "usage",
            "compact",
            "clear",
            "context",
            "tools",
            "allow",
            "deny",
            "allows",
            "audit",
            "export",
            "debug",
        ]
        names = registry.all_command_names()
        for cmd in expected:
            assert cmd in names, f"/{cmd} not registered"

    def test_reset_alias_for_clear(self):
        registry = build_default_registry()
        assert registry.get_command("reset") is not None

    def test_help_returns_text(self):
        registry = build_default_registry()
        text = registry.help_text()
        assert "/help" in text
        assert "/status" in text


# --- Built-in command handlers ---


class TestBuiltinHandlers:
    @pytest.fixture
    def server(self):
        """Minimal mock ChatServer for command handlers."""
        srv = MagicMock()
        srv._command_registry = build_default_registry()
        srv._agent_def = MagicMock()
        srv._agent_def.tools = {}
        srv._agent_def.workspace.path = "/tmp/test"
        srv._agent_def.workspace.memory_context_mode = "recent"
        srv._agent_def.workspace.memory_days = 3
        srv._agent_def.workspace.max_chars_per_file = 5000
        srv._agent_def.guardian = None
        srv._guardian = None
        srv._session_mgr = MagicMock()
        srv._session_states = {}
        return srv

    def test_cmd_help(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/help", ctx)
        assert "/help" in result
        assert "/status" in result

    def test_cmd_help_specific(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/help status", ctx)
        assert "/status" in result
        assert "Show server status" in result

    def test_cmd_new(self, server):
        session = MagicMock()
        session.session_id = "abc123"
        server._session_mgr.new_session.return_value = session
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/new", ctx)
        assert "abc123" in result

    def test_cmd_clear(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/clear", ctx)
        assert "cleared" in result.lower()
        server._session_mgr.clear.assert_called_once_with("test")

    def test_cmd_reset_alias(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/reset", ctx)
        assert "cleared" in result.lower()

    def test_cmd_tools_empty(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/tools", ctx)
        assert "No tools" in result

    def test_cmd_tools_with_tools(self, server):
        tool_cfg = MagicMock()
        tool_cfg.executor = "weather"
        tool_cfg.network = True
        server._agent_def.tools = {"check_weather": tool_cfg}
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/tools", ctx)
        assert "check_weather" in result
        assert "1 tools" in result

    def test_cmd_usage(self, server):
        session = MagicMock()
        session.session_id = "sess123"
        session.token_count = 5000
        session.messages = [{"role": "user", "content": "hi"}]
        server._session_mgr.get_or_create.return_value = session
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/usage", ctx)
        assert "5000" in result
        assert "sess123" in result

    def test_cmd_export_empty(self, server):
        session = MagicMock()
        session.messages = []
        server._session_mgr.get_or_create.return_value = session
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/export", ctx)
        assert "No messages" in result

    def test_cmd_export_with_messages(self, server):
        session = MagicMock()
        session.session_id = "sess456"
        session.messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        server._session_mgr.get_or_create.return_value = session
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/export", ctx)
        assert "Hello" in result
        assert "Hi there!" in result
        assert "sess456" in result

    def test_cmd_debug_no_guardian(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/debug", ctx)
        assert "No guardian" in result

    def test_cmd_debug_toggle(self, server):
        server._agent_def.guardian = MagicMock()
        server._agent_def.guardian.debug = False
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/debug", ctx)
        assert "ON" in result

    def test_cmd_context(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        with patch("pathlib.Path.is_dir", return_value=False):
            result = server._command_registry.handle("/context", ctx)
        assert "Context injection" in result
        assert "recent" in result

    def test_cmd_audit_no_guardian(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/audit", ctx)
        assert "not enabled" in result

    def test_unknown_command(self, server):
        ctx = ChatContext(sender_id="test", server=server)
        result = server._command_registry.handle("/foobar", ctx)
        assert "Unknown command" in result
        assert "/help" in result


# --- Integration: ChatServer.handle_message dispatches through registry ---


class TestChatServerRegistryIntegration:
    @pytest.fixture
    def chat_server(self):
        """Build a minimal ChatServer with registry."""
        with patch("creel.chat.ChatServer.__init__", return_value=None):
            from creel.chat import ChatServer

            server = ChatServer.__new__(ChatServer)
        server._command_registry = build_default_registry()
        server._guardian = None
        server._agent_def = MagicMock()
        server._agent_def.tools = {}
        server._agent_def.guardian = None
        server._session_mgr = MagicMock()
        server._session_states = {}
        server._approval_queue = MagicMock()
        server._approval_queue.get_pending.return_value = None
        return server

    def test_slash_command_dispatched(self, chat_server):
        session = MagicMock()
        session.session_id = "test-session"
        chat_server._session_mgr.new_session.return_value = session
        result = chat_server.handle_message("user1", "/new")
        assert "test-session" in result

    def test_unknown_slash_command(self, chat_server):
        result = chat_server.handle_message("user1", "/nonexistent")
        assert "Unknown command" in result
