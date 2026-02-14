"""Tests for contextual policy rules (deny_when / review_when)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from guardian.policy import PolicyEngine, _match_condition
from guardian.types import ActionVerdict


class TestMatchCondition:
    def test_exact_match(self) -> None:
        assert _match_condition(
            {"arg": "to", "pattern": "evil@hacker.com"},
            {"to": "evil@hacker.com"},
        )

    def test_glob_match(self) -> None:
        assert _match_condition(
            {"arg": "to", "pattern": "*@external.com"},
            {"to": "user@external.com"},
        )

    def test_no_match(self) -> None:
        assert not _match_condition(
            {"arg": "to", "pattern": "*@external.com"},
            {"to": "user@internal.com"},
        )

    def test_missing_arg(self) -> None:
        assert not _match_condition(
            {"arg": "to", "pattern": "*@external.com"},
            {"subject": "hello"},
        )

    def test_empty_condition(self) -> None:
        assert not _match_condition({}, {"to": "test"})

    def test_empty_pattern(self) -> None:
        assert not _match_condition({"arg": "to", "pattern": ""}, {"to": "test"})

    def test_non_string_value(self) -> None:
        """Non-string values should be coerced to string."""
        assert _match_condition(
            {"arg": "count", "pattern": "42"},
            {"count": 42},
        )


@pytest.fixture
def contextual_policy(tmp_path: Path) -> PolicyEngine:
    p = tmp_path / "policy.yaml"
    p.write_text(textwrap.dedent("""\
        allow:
          - check_weather
          - send_email

        review:
          - upload_*

        deny:
          - delete_*

        deny_when:
          - tool: send_email
            arg: to
            pattern: "*@external.com"
          - tool: send_email
            arg: to
            pattern: "*@competitor.com"

        review_when:
          - tool: upload_*
            arg: visibility
            pattern: "public"
    """))
    return PolicyEngine(p)


class TestDenyWhen:
    def test_deny_when_matches(self, contextual_policy: PolicyEngine) -> None:
        decision = contextual_policy.evaluate("send_email", {"to": "spy@external.com"})
        assert decision.verdict == ActionVerdict.DENY
        assert "deny_when" in decision.matched_rule
        assert "external.com" in decision.reason

    def test_deny_when_second_rule(self, contextual_policy: PolicyEngine) -> None:
        decision = contextual_policy.evaluate("send_email", {"to": "info@competitor.com"})
        assert decision.verdict == ActionVerdict.DENY

    def test_deny_when_no_match_falls_through(self, contextual_policy: PolicyEngine) -> None:
        decision = contextual_policy.evaluate("send_email", {"to": "friend@internal.com"})
        assert decision.verdict == ActionVerdict.ALLOW

    def test_deny_when_missing_arg(self, contextual_policy: PolicyEngine) -> None:
        decision = contextual_policy.evaluate("send_email", {"subject": "hello"})
        assert decision.verdict == ActionVerdict.ALLOW

    def test_deny_when_no_args(self, contextual_policy: PolicyEngine) -> None:
        decision = contextual_policy.evaluate("send_email")
        assert decision.verdict == ActionVerdict.ALLOW

    def test_deny_when_tool_no_match(self, contextual_policy: PolicyEngine) -> None:
        """deny_when for send_email should not affect other tools."""
        decision = contextual_policy.evaluate("check_weather", {"to": "spy@external.com"})
        assert decision.verdict == ActionVerdict.ALLOW

    def test_static_deny_still_works(self, contextual_policy: PolicyEngine) -> None:
        decision = contextual_policy.evaluate("delete_file", {})
        assert decision.verdict == ActionVerdict.DENY


class TestReviewWhen:
    def test_review_when_matches(self, contextual_policy: PolicyEngine) -> None:
        decision = contextual_policy.evaluate("upload_doc", {"visibility": "public"})
        # upload_* matches review statically AND review_when — review either way
        assert decision.verdict == ActionVerdict.REVIEW

    def test_review_when_no_match(self, contextual_policy: PolicyEngine) -> None:
        decision = contextual_policy.evaluate("upload_doc", {"visibility": "private"})
        # Still matches static review for upload_*
        assert decision.verdict == ActionVerdict.REVIEW


class TestBackwardCompatibility:
    def test_evaluate_without_args(self, contextual_policy: PolicyEngine) -> None:
        """Calling evaluate with just tool_name should still work."""
        decision = contextual_policy.evaluate("check_weather")
        assert decision.verdict == ActionVerdict.ALLOW

    def test_old_style_policy_still_works(self, tmp_path: Path) -> None:
        """Policy without deny_when/review_when should work as before."""
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
        assert engine.evaluate("check_weather").verdict == ActionVerdict.ALLOW
        assert engine.evaluate("send_email").verdict == ActionVerdict.REVIEW
        assert engine.evaluate("delete_all").verdict == ActionVerdict.DENY
