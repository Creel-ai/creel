"""Policy engine — YAML-based allow/review/deny rules for tool actions."""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path

import yaml

from guardian.types import ActionDecision, ActionVerdict

logger = logging.getLogger(__name__)


def _match_condition(condition: dict, tool_args: dict) -> bool:
    """Check if a deny_when/review_when condition matches the tool args.

    Condition format:
        {"arg": "<arg_name>", "pattern": "<glob_pattern>"}

    The pattern is matched using fnmatch (glob-style) against the string
    value of the specified argument.  If the arg is missing from tool_args,
    the condition does not match.
    """
    arg_name = condition.get("arg", "")
    pattern = condition.get("pattern", "")

    if not arg_name or not pattern:
        return False

    value = tool_args.get(arg_name)
    if value is None:
        return False

    return fnmatch.fnmatch(str(value), pattern)


class PolicyEngine:
    """Evaluates tool actions against allow/review/deny rules.

    Evaluation order: deny → conditional deny_when → review →
    conditional review_when → allow (most restrictive wins).
    Unknown tools default to ``review``.

    Conditional rules (``deny_when``, ``review_when``) match on tool
    input arguments::

        deny_when:
          - tool: send_email
            arg: to
            pattern: "*@external.com"

        review_when:
          - tool: upload_*
            arg: visibility
            pattern: "public"
    """

    def __init__(self, policy_file: str | Path) -> None:
        self._deny: list[str] = []
        self._review: list[str] = []
        self._allow: list[str] = []
        self._deny_when: list[dict] = []
        self._review_when: list[dict] = []
        self._load(Path(policy_file))

    def _load(self, path: Path) -> None:
        if not path.exists():
            logger.warning("Policy file not found: %s — using empty policy", path)
            return

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        self._deny = data.get("deny", [])
        self._review = data.get("review", [])
        self._allow = data.get("allow", [])
        self._deny_when = data.get("deny_when", [])
        self._review_when = data.get("review_when", [])

        logger.info(
            "Loaded policy: %d deny, %d review, %d allow, %d deny_when, %d review_when rules",
            len(self._deny),
            len(self._review),
            len(self._allow),
            len(self._deny_when),
            len(self._review_when),
        )

    def evaluate(self, tool_name: str, tool_args: dict | None = None) -> ActionDecision:
        """Evaluate a tool name (and optionally args) against the loaded policy rules.

        Returns an ActionDecision with the verdict and matched rule.
        """
        if tool_args is None:
            tool_args = {}

        # Check deny first (most restrictive)
        for pattern in self._deny:
            if fnmatch.fnmatch(tool_name, pattern):
                return ActionDecision(
                    verdict=ActionVerdict.DENY,
                    tool_name=tool_name,
                    matched_rule=pattern,
                    reason=f"Tool '{tool_name}' denied by policy rule '{pattern}'",
                )

        # Conditional deny_when rules
        for rule in self._deny_when:
            tool_pattern = rule.get("tool", "")
            if fnmatch.fnmatch(tool_name, tool_pattern) and _match_condition(rule, tool_args):
                rule_desc = f"deny_when:{tool_pattern}:{rule.get('arg')}={rule.get('pattern')}"
                return ActionDecision(
                    verdict=ActionVerdict.DENY,
                    tool_name=tool_name,
                    matched_rule=rule_desc,
                    reason=f"Tool '{tool_name}' denied by conditional rule (arg '{rule.get('arg')}' matches '{rule.get('pattern')}')",
                )

        # Then review
        for pattern in self._review:
            if fnmatch.fnmatch(tool_name, pattern):
                return ActionDecision(
                    verdict=ActionVerdict.REVIEW,
                    tool_name=tool_name,
                    matched_rule=pattern,
                    reason=f"Tool '{tool_name}' flagged for review by rule '{pattern}'",
                )

        # Conditional review_when rules
        for rule in self._review_when:
            tool_pattern = rule.get("tool", "")
            if fnmatch.fnmatch(tool_name, tool_pattern) and _match_condition(rule, tool_args):
                rule_desc = f"review_when:{tool_pattern}:{rule.get('arg')}={rule.get('pattern')}"
                return ActionDecision(
                    verdict=ActionVerdict.REVIEW,
                    tool_name=tool_name,
                    matched_rule=rule_desc,
                    reason=f"Tool '{tool_name}' flagged for review by conditional rule (arg '{rule.get('arg')}' matches '{rule.get('pattern')}')",
                )

        # Then allow
        for pattern in self._allow:
            if fnmatch.fnmatch(tool_name, pattern):
                return ActionDecision(
                    verdict=ActionVerdict.ALLOW,
                    tool_name=tool_name,
                    matched_rule=pattern,
                    reason=f"Tool '{tool_name}' allowed by rule '{pattern}'",
                )

        # Unknown tool — default to review
        return ActionDecision(
            verdict=ActionVerdict.REVIEW,
            tool_name=tool_name,
            matched_rule="",
            reason=f"Tool '{tool_name}' not in policy — defaulting to review",
        )
