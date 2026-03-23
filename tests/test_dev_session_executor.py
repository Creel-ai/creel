"""Tests for dev_session executor skill registration."""

from __future__ import annotations

import pytest


class TestSkillRegistration:
    """Test that dev_session skill is registered correctly."""

    def test_register_skill_returns_valid_meta(self):
        from executors.dev_session.executor import register_skill

        meta, execute = register_skill()
        assert meta.id == "dev_session"
        assert meta.needs_network is True
        assert callable(execute)

    def test_tool_names(self):
        from executors.dev_session.executor import register_skill

        meta, _ = register_skill()
        tool_names = [t.name for t in meta.tools]
        assert "dev_exec" in tool_names
        assert "dev_process" in tool_names
        assert "dev_sessions" in tool_names

    def test_dev_exec_params(self):
        from executors.dev_session.executor import register_skill

        meta, _ = register_skill()
        dev_exec = next(t for t in meta.tools if t.name == "dev_exec")
        param_names = [p.name for p in dev_exec.params]
        assert "command" in param_names
        assert "background" in param_names
        assert "workdir" in param_names
        assert "timeout" in param_names

        command_param = next(p for p in dev_exec.params if p.name == "command")
        assert command_param.required is True

    def test_dev_process_params(self):
        from executors.dev_session.executor import register_skill

        meta, _ = register_skill()
        dev_process = next(t for t in meta.tools if t.name == "dev_process")
        param_names = [p.name for p in dev_process.params]
        assert "session_id" in param_names
        assert "action" in param_names
        assert "limit" in param_names
        assert "offset" in param_names
        assert "data" in param_names

        session_param = next(p for p in dev_process.params if p.name == "session_id")
        assert session_param.required is True

    def test_dev_sessions_has_no_params(self):
        from executors.dev_session.executor import register_skill

        meta, _ = register_skill()
        dev_sessions = next(t for t in meta.tools if t.name == "dev_sessions")
        assert len(dev_sessions.params) == 0

    def test_fixed_args(self):
        from executors.dev_session.executor import register_skill

        meta, _ = register_skill()
        tools = {t.name: t for t in meta.tools}
        assert tools["dev_exec"].fixed_args == {"_action": "exec"}
        assert tools["dev_process"].fixed_args == {"_action": "process"}
        assert tools["dev_sessions"].fixed_args == {"_action": "sessions"}

    def test_execute_raises_for_inline_mode(self):
        from executors.dev_session.executor import register_skill

        _, execute = register_skill()
        with pytest.raises(RuntimeError, match="container mode"):
            execute(None)
