"""Policy engine — YAML-based allow/review/deny rules for tool actions."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

import yaml

from guardian.types import ActionDecision, ActionVerdict

logger = logging.getLogger(__name__)


class PolicyEngine:
    """Evaluates tool actions against allow/review/deny rules.

    Evaluation order: deny → review → allow (most restrictive wins).
    Unknown tools default to ``review``.
    """

    def __init__(self, policy_file: str | Path) -> None:
        self._deny: list[str] = []
        self._review: list[str] = []
        self._allow: list[str] = []
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

        logger.info(
            "Loaded policy: %d deny, %d review, %d allow rules",
            len(self._deny),
            len(self._review),
            len(self._allow),
        )

    def evaluate(self, tool_name: str) -> ActionDecision:
        """Evaluate a tool name against the loaded policy rules.

        Returns an ActionDecision with the verdict and matched rule.
        """
        # Check deny first (most restrictive)
        for pattern in self._deny:
            if fnmatch.fnmatch(tool_name, pattern):
                return ActionDecision(
                    verdict=ActionVerdict.DENY,
                    tool_name=tool_name,
                    matched_rule=pattern,
                    reason=f"Tool '{tool_name}' denied by policy rule '{pattern}'",
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
