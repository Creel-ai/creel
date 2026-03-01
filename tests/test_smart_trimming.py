"""Tests for smart session trimming that preserves tool-call pairs."""

from __future__ import annotations

from pathlib import Path

from taskrunner.session import SessionManager


def _tool_call_conversation() -> list[dict]:
    """Build a conversation with tool calls for testing."""
    return [
        {"role": "user", "content": "What's the weather?"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "weather", "input": {"city": "Denver"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Sunny, 72°F"},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "It's sunny!"}]},
        {"role": "user", "content": "Thanks"},
        {"role": "assistant", "content": [{"type": "text", "text": "You're welcome!"}]},
        {"role": "user", "content": "What about tomorrow?"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "weather",
                    "input": {"city": "Denver", "day": "tomorrow"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "Partly cloudy, 65°F"},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Partly cloudy tomorrow!"}]},
    ]


class TestSmartTrimming:
    """Tests that trimming never breaks mid-tool-call."""

    def test_trim_starts_at_user_text(self, tmp_path: Path):
        """Trimmed history should start with a user text message, not a tool_result."""
        mgr = SessionManager(sessions_dir=str(tmp_path), max_history=6)
        session = mgr.get_or_create("cli")
        session.messages = _tool_call_conversation()  # 10 messages
        mgr._save(session)

        loaded = mgr.get_or_create("cli")
        assert loaded.messages[0]["role"] == "user"
        assert isinstance(loaded.messages[0]["content"], str)

    def test_trim_never_starts_with_tool_result(self, tmp_path: Path):
        """With max_history=5, naive trim would start at tool_result. Smart trim skips it."""
        mgr = SessionManager(sessions_dir=str(tmp_path), max_history=5)
        session = mgr.get_or_create("cli")
        session.messages = _tool_call_conversation()
        mgr._save(session)

        loaded = mgr.get_or_create("cli")
        first = loaded.messages[0]
        # Should never be a tool_result
        assert not (first["role"] == "user" and isinstance(first.get("content"), list))
        assert first["role"] == "user"
        assert isinstance(first["content"], str)

    def test_trim_never_starts_with_assistant(self, tmp_path: Path):
        """Trimmed history should never start with an assistant message."""
        mgr = SessionManager(sessions_dir=str(tmp_path), max_history=3)
        session = mgr.get_or_create("cli")
        session.messages = _tool_call_conversation()
        mgr._save(session)

        loaded = mgr.get_or_create("cli")
        if loaded.messages:
            assert loaded.messages[0]["role"] == "user"

    def test_no_trimming_when_under_limit(self, tmp_path: Path):
        """Messages under max_history should not be modified."""
        mgr = SessionManager(sessions_dir=str(tmp_path), max_history=50)
        session = mgr.get_or_create("cli")
        session.messages = _tool_call_conversation()
        mgr._save(session)

        loaded = mgr.get_or_create("cli")
        assert len(loaded.messages) == 10

    def test_trim_preserves_complete_tool_pairs(self, tmp_path: Path):
        """After trimming, every tool_use should have a matching tool_result."""
        mgr = SessionManager(sessions_dir=str(tmp_path), max_history=7)
        session = mgr.get_or_create("cli")
        session.messages = _tool_call_conversation()
        mgr._save(session)

        loaded = mgr.get_or_create("cli")
        # Collect tool_use IDs and tool_result IDs
        tool_use_ids = set()
        tool_result_ids = set()
        for msg in loaded.messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            tool_use_ids.add(block["id"])
                        elif block.get("type") == "tool_result":
                            tool_result_ids.add(block["tool_use_id"])
        # Every tool_use should have a matching result
        assert tool_use_ids == tool_result_ids

    def test_consecutive_tool_calls(self, tmp_path: Path):
        """Multiple consecutive tool calls should be kept as complete pairs."""
        mgr = SessionManager(sessions_dir=str(tmp_path), max_history=4)
        messages = [
            {"role": "user", "content": "Do two things"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "a", "name": "tool_a", "input": {}},
                    {"type": "tool_use", "id": "b", "name": "tool_b", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "result_a"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "result_b"},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Done!"}]},
            {"role": "user", "content": "Great"},
            {"role": "assistant", "content": [{"type": "text", "text": "Thanks!"}]},
        ]
        session = mgr.get_or_create("cli")
        session.messages = messages
        mgr._save(session)

        loaded = mgr.get_or_create("cli")
        assert loaded.messages[0]["role"] == "user"
        assert isinstance(loaded.messages[0]["content"], str)
