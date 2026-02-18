"""Tests for per-tool review overrides (auto_approve)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from guardian.policy import PolicyEngine
from guardian.types import ActionVerdict


@pytest.fixture
def policy_with_auto_approve(tmp_path: Path) -> PolicyEngine:
    p = tmp_path / "policy.yaml"
    p.write_text(textwrap.dedent("""\
        allow:
          - check_weather
          - check_email

        review:
          - send_*
          - mark_*
          - react_*

        deny:
          - delete_*

        auto_approve:
          - mark_read
          - react_imessage
    """))
    return PolicyEngine(p)


class TestAutoApprove:
    def test_auto_approve_skips_review(self, policy_with_auto_approve: PolicyEngine) -> None:
        decision = policy_with_auto_approve.evaluate("mark_read")
        assert decision.verdict == ActionVerdict.ALLOW
        assert "auto_approve" in decision.matched_rule

    def test_auto_approve_react(self, policy_with_auto_approve: PolicyEngine) -> None:
        decision = policy_with_auto_approve.evaluate("react_imessage")
        assert decision.verdict == ActionVerdict.ALLOW
        assert "auto_approve" in decision.matched_rule

    def test_non_auto_approve_still_review(self, policy_with_auto_approve: PolicyEngine) -> None:
        decision = policy_with_auto_approve.evaluate("mark_unread")
        assert decision.verdict == ActionVerdict.REVIEW

    def test_send_email_still_review(self, policy_with_auto_approve: PolicyEngine) -> None:
        decision = policy_with_auto_approve.evaluate("send_email")
        assert decision.verdict == ActionVerdict.REVIEW

    def test_deny_not_overridden(self, policy_with_auto_approve: PolicyEngine) -> None:
        """auto_approve should NOT override deny rules."""
        decision = policy_with_auto_approve.evaluate("delete_file")
        assert decision.verdict == ActionVerdict.DENY

    def test_allow_unaffected(self, policy_with_auto_approve: PolicyEngine) -> None:
        decision = policy_with_auto_approve.evaluate("check_weather")
        assert decision.verdict == ActionVerdict.ALLOW
        assert "auto_approve" not in decision.matched_rule


class TestAutoApproveGlob:
    def test_glob_auto_approve(self, tmp_path: Path) -> None:
        p = tmp_path / "policy.yaml"
        p.write_text(textwrap.dedent("""\
            review:
              - mark_*
            auto_approve:
              - mark_*
        """))
        engine = PolicyEngine(p)
        decision = engine.evaluate("mark_read")
        assert decision.verdict == ActionVerdict.ALLOW
        assert "auto_approve" in decision.matched_rule

    def test_unknown_tool_with_auto_approve(self, tmp_path: Path) -> None:
        p = tmp_path / "policy.yaml"
        p.write_text(textwrap.dedent("""\
            review:
              - send_*
            auto_approve:
              - my_custom_tool
        """))
        engine = PolicyEngine(p)
        # my_custom_tool is unknown (not in review/allow/deny) but in auto_approve
        decision = engine.evaluate("my_custom_tool")
        assert decision.verdict == ActionVerdict.ALLOW
        assert "auto_approve" in decision.matched_rule


class TestNoAutoApprove:
    def test_policy_without_auto_approve(self, tmp_path: Path) -> None:
        p = tmp_path / "policy.yaml"
        p.write_text(textwrap.dedent("""\
            allow:
              - check_weather
            review:
              - send_*
            deny:
              - delete_*
        """))
        engine = PolicyEngine(p)
        decision = engine.evaluate("send_email")
        assert decision.verdict == ActionVerdict.REVIEW


class TestDefaultPolicy:
    def test_default_policy_auto_approve(self) -> None:
        """Verify the default policy file has auto_approve entries."""
        engine = PolicyEngine("policies/default.yaml")
        # mark_read should be auto-approved
        decision = engine.evaluate("mark_read")
        assert decision.verdict == ActionVerdict.ALLOW
        # send_email should still require review
        decision = engine.evaluate("send_email")
        assert decision.verdict == ActionVerdict.REVIEW


class TestMemoryToolPolicy:
    """Verify memory tools have explicit policy entries."""

    def test_remember_allowed(self) -> None:
        engine = PolicyEngine("policies/default.yaml")
        decision = engine.evaluate("remember")
        assert decision.verdict == ActionVerdict.ALLOW

    def test_search_memory_allowed(self) -> None:
        engine = PolicyEngine("policies/default.yaml")
        decision = engine.evaluate("search_memory")
        assert decision.verdict == ActionVerdict.ALLOW

    def test_list_memory_files_allowed(self) -> None:
        engine = PolicyEngine("policies/default.yaml")
        decision = engine.evaluate("list_memory_files")
        assert decision.verdict == ActionVerdict.ALLOW

    def test_update_long_term_memory_allowed(self) -> None:
        engine = PolicyEngine("policies/default.yaml")
        decision = engine.evaluate("update_long_term_memory")
        assert decision.verdict == ActionVerdict.ALLOW

    def test_edit_memory_requires_review(self) -> None:
        engine = PolicyEngine("policies/default.yaml")
        decision = engine.evaluate("edit_memory")
        assert decision.verdict == ActionVerdict.REVIEW

    def test_delete_memory_denied(self) -> None:
        engine = PolicyEngine("policies/default.yaml")
        decision = engine.evaluate("delete_memory")
        assert decision.verdict == ActionVerdict.DENY


class TestNotionToolPolicy:
    def test_notion_read_action_allowed(self) -> None:
        engine = PolicyEngine("policies/default.yaml")
        decision = engine.evaluate("notion_api", {"action": "search"})
        assert decision.verdict == ActionVerdict.ALLOW

    def test_notion_write_like_action_requires_review(self) -> None:
        engine = PolicyEngine("policies/default.yaml")
        decision = engine.evaluate("notion_api", {"action": "create_page"})
        assert decision.verdict == ActionVerdict.REVIEW
