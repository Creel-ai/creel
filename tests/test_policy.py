"""Tests for the policy engine."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from guardian.policy import PolicyEngine
from guardian.types import ActionVerdict


@pytest.fixture
def policy_file(tmp_path: Path) -> Path:
    """Create a temporary policy file."""
    p = tmp_path / "policy.yaml"
    p.write_text(textwrap.dedent("""\
        allow:
          - check_weather
          - check_calendar
          - check_email
          - check_drive

        review:
          - send_*
          - upload_*
          - create_*
          - mark_*

        deny:
          - trash_*
          - delete_*
    """))
    return p


@pytest.fixture
def engine(policy_file: Path) -> PolicyEngine:
    return PolicyEngine(policy_file)


class TestPolicyEngine:
    def test_allow_exact_match(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("check_weather")
        assert decision.verdict == ActionVerdict.ALLOW
        assert decision.matched_rule == "check_weather"

    def test_allow_other_exact(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("check_email")
        assert decision.verdict == ActionVerdict.ALLOW

    def test_deny_glob(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("trash_email")
        assert decision.verdict == ActionVerdict.DENY
        assert decision.matched_rule == "trash_*"

    def test_deny_another_glob(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("delete_file")
        assert decision.verdict == ActionVerdict.DENY

    def test_review_glob(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("send_email")
        assert decision.verdict == ActionVerdict.REVIEW
        assert decision.matched_rule == "send_*"

    def test_review_upload_glob(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("upload_file")
        assert decision.verdict == ActionVerdict.REVIEW

    def test_review_create_glob(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("create_event")
        assert decision.verdict == ActionVerdict.REVIEW

    def test_unknown_tool_defaults_to_review(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("totally_unknown_tool")
        assert decision.verdict == ActionVerdict.REVIEW
        assert decision.matched_rule == ""
        assert "defaulting to review" in decision.reason

    def test_deny_wins_over_allow(self, tmp_path: Path) -> None:
        """If a tool matches both deny and allow, deny wins."""
        p = tmp_path / "conflict.yaml"
        p.write_text(textwrap.dedent("""\
            allow:
              - delete_temp
            deny:
              - delete_*
        """))
        eng = PolicyEngine(p)
        decision = eng.evaluate("delete_temp")
        assert decision.verdict == ActionVerdict.DENY

    def test_deny_wins_over_review(self, tmp_path: Path) -> None:
        """If a tool matches both deny and review, deny wins."""
        p = tmp_path / "conflict.yaml"
        p.write_text(textwrap.dedent("""\
            review:
              - trash_*
            deny:
              - trash_*
        """))
        eng = PolicyEngine(p)
        decision = eng.evaluate("trash_email")
        assert decision.verdict == ActionVerdict.DENY

    def test_review_wins_over_allow(self, tmp_path: Path) -> None:
        """If a tool matches both review and allow, review wins."""
        p = tmp_path / "conflict.yaml"
        p.write_text(textwrap.dedent("""\
            allow:
              - send_*
            review:
              - send_*
        """))
        eng = PolicyEngine(p)
        decision = eng.evaluate("send_email")
        assert decision.verdict == ActionVerdict.REVIEW

    def test_missing_policy_file(self, tmp_path: Path) -> None:
        """Missing policy file should not crash — empty policy, everything is review."""
        eng = PolicyEngine(tmp_path / "nonexistent.yaml")
        decision = eng.evaluate("check_weather")
        assert decision.verdict == ActionVerdict.REVIEW

    def test_empty_policy_file(self, tmp_path: Path) -> None:
        """Empty policy file should not crash."""
        p = tmp_path / "empty.yaml"
        p.write_text("")
        eng = PolicyEngine(p)
        decision = eng.evaluate("check_weather")
        assert decision.verdict == ActionVerdict.REVIEW

    def test_tool_name_in_decision(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate("check_drive")
        assert decision.tool_name == "check_drive"
