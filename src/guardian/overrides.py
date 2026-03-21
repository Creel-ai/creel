"""Temporary policy overrides — time-limited allow/deny rules."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch

from guardian.audit import AuditLogger
from guardian.types import ActionDecision, ActionVerdict, OverrideConfig

logger = logging.getLogger(__name__)

# Pattern for parsing duration strings: 1h30m, 30m, 90s, 2h
_DURATION_RE = re.compile(
    r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$",
    re.IGNORECASE,
)

# Pattern for extracting use-count prefix: "10x", "5x 30m"
_USE_COUNT_RE = re.compile(r"^(\d+)x\s*(.*)", re.IGNORECASE)


@dataclass
class TemporaryOverride:
    """A time-limited policy override for a tool pattern."""

    id: str
    pattern: str
    action: ActionVerdict  # ALLOW or DENY
    expires_at: datetime
    created_at: datetime
    created_by: str
    scope: str = ""
    use_count: int = 0
    max_uses: int | None = None  # None = unlimited

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    @property
    def is_exhausted(self) -> bool:
        return self.max_uses is not None and self.use_count >= self.max_uses

    @property
    def is_active(self) -> bool:
        return not self.is_expired and not self.is_exhausted

    @property
    def remaining_seconds(self) -> int:
        delta = self.expires_at - datetime.now(UTC)
        return max(0, int(delta.total_seconds()))


def parse_duration(spec: str) -> int:
    """Parse a duration string into seconds.

    Supports: ``30m``, ``2h``, ``1h30m``, ``90s``, ``1h30m45s``.
    Raises ``ValueError`` for invalid formats.
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("Empty duration")

    m = _DURATION_RE.match(spec)
    if not m or not any(m.groups()):
        raise ValueError(f"Invalid duration format: {spec!r}")

    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"Duration must be positive: {spec!r}")
    return total


def parse_use_count(spec: str) -> tuple[int | None, str]:
    """Extract a use-count prefix from a spec string.

    Returns ``(max_uses, remaining_spec)``.
    ``max_uses`` is ``None`` if no count prefix is found.

    Examples:
        ``"10x"`` → ``(10, "")``
        ``"5x 30m"`` → ``(5, "30m")``
        ``"30m"`` → ``(None, "30m")``
    """
    m = _USE_COUNT_RE.match(spec.strip())
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, spec.strip()


class TemporaryOverrideManager:
    """Manages time-limited policy overrides with thread-safe access."""

    def __init__(
        self,
        config: OverrideConfig,
        audit: AuditLogger | None = None,
    ) -> None:
        self._config = config
        self._audit = audit
        self._overrides: dict[str, TemporaryOverride] = {}
        self._lock = threading.Lock()

    @property
    def requires_wildcard_confirmation(self) -> bool:
        """Whether bare wildcard patterns require explicit confirmation."""
        return self._config.require_confirmation_for_wildcard

    def create_override(
        self,
        pattern: str,
        action: ActionVerdict,
        duration_seconds: int,
        created_by: str,
        scope: str = "",
        max_uses: int | None = None,
    ) -> TemporaryOverride:
        """Create a new temporary override.

        Raises ``ValueError`` if the pattern matches excluded tools,
        if the duration exceeds the cap, or if the pattern is a bare
        wildcard and ``require_confirmation_for_wildcard`` is set.
        """
        if not self._config.enabled:
            raise ValueError("Temporary overrides are disabled")

        # Enforce max active overrides to prevent resource exhaustion
        with self._lock:
            active_count = sum(1 for ov in self._overrides.values() if ov.is_active)
            if active_count >= self._config.max_active_overrides:
                raise ValueError(
                    f"Maximum active overrides ({self._config.max_active_overrides}) reached. "
                    "Revoke an existing override first."
                )

        # Check excluded tools — for wildcards like "*", silently skip
        # excluded patterns rather than rejecting outright.  For specific
        # patterns that target excluded tools (e.g. "del*"), still reject.
        if pattern == "*":
            # Wildcard allows everything EXCEPT excluded tools.
            # The match() method already respects excluded_tools, so this
            # just needs to pass through.  Log a note so the user knows.
            excluded_str = ", ".join(self._config.excluded_tools)
            logger.info("Wildcard override excludes: %s", excluded_str)
        else:
            for excluded in self._config.excluded_tools:
                if fnmatch(pattern, excluded) or fnmatch(excluded, pattern):
                    raise ValueError(
                        f"Pattern {pattern!r} matches excluded tool pattern {excluded!r}"
                    )

        # Cap duration
        max_seconds = int(self._config.absolute_max_duration_hours * 3600)
        if duration_seconds > max_seconds:
            raise ValueError(
                f"Duration {duration_seconds}s exceeds maximum "
                f"({self._config.absolute_max_duration_hours}h)"
            )

        now = datetime.now(UTC)
        override = TemporaryOverride(
            id=uuid.uuid4().hex[:12],
            pattern=pattern,
            action=action,
            expires_at=now + timedelta(seconds=duration_seconds),
            created_at=now,
            created_by=created_by,
            scope=scope,
            max_uses=max_uses,
        )

        with self._lock:
            self._overrides[override.id] = override

        if self._audit:
            self._audit.log_override_created(
                override_id=override.id,
                pattern=pattern,
                action=action.value,
                duration_seconds=duration_seconds,
                created_by=created_by,
                scope=scope,
                max_uses=max_uses,
            )

        logger.info(
            "Override created: %s %s (expires in %ds, max_uses=%s)",
            action.value,
            pattern,
            duration_seconds,
            max_uses,
        )
        return override

    def revoke_override(self, pattern: str) -> TemporaryOverride | None:
        """Revoke an active override matching the given pattern.

        Returns the revoked override, or ``None`` if no match was found.
        """
        with self._lock:
            for oid, override in list(self._overrides.items()):
                if override.pattern == pattern and override.is_active:
                    del self._overrides[oid]
                    if self._audit:
                        self._audit.log_override_revoked(
                            override_id=override.id,
                            pattern=pattern,
                            revoked_by="user",
                        )
                    logger.info("Override revoked: %s %s", override.action.value, pattern)
                    return override
        return None

    def _gc_expired(self) -> list[str]:
        """Remove expired/exhausted overrides and audit-log them.

        Must be called while ``self._lock`` is held. Returns the list of
        removed override IDs.
        """
        expired_ids = [
            oid for oid, ov in self._overrides.items() if ov.is_expired or ov.is_exhausted
        ]
        for oid in expired_ids:
            ov = self._overrides.pop(oid)
            if self._audit:
                self._audit.log_override_expired(
                    override_id=ov.id,
                    pattern=ov.pattern,
                    use_count=ov.use_count,
                )
        return expired_ids

    def list_active(self) -> list[TemporaryOverride]:
        """Return all active overrides, garbage-collecting expired ones."""
        with self._lock:
            self._gc_expired()
            return [ov for ov in self._overrides.values() if ov.is_active]

    def check(
        self, tool_name: str, tool_args: dict, *, sender_id: str = ""
    ) -> ActionDecision | None:
        """Check if a tool call matches any active override.

        Deny overrides take priority over allow overrides.
        When ``sender_id`` is provided, only overrides created by that
        sender (or with no scope restriction) are considered.
        Returns ``None`` if no override matches (fall through to static policy).
        """
        deny_match: TemporaryOverride | None = None
        allow_match: TemporaryOverride | None = None

        with self._lock:
            self._gc_expired()

            for override in self._overrides.values():
                if not override.is_active:
                    continue
                # Scope check: skip overrides from a different sender
                if sender_id and override.created_by and override.created_by != sender_id:
                    continue
                if fnmatch(tool_name, override.pattern):
                    # Wildcard overrides skip excluded tools
                    if override.pattern == "*" and override.action == ActionVerdict.ALLOW:
                        if any(fnmatch(tool_name, exc) for exc in self._config.excluded_tools):
                            continue
                    if override.action == ActionVerdict.DENY:
                        deny_match = override
                    elif override.action == ActionVerdict.ALLOW and deny_match is None:
                        allow_match = override

            # Deny wins over allow
            matched = deny_match or allow_match
            if matched is None:
                return None

            matched.use_count += 1

            # Check if this use exhausted the override
            if matched.is_exhausted:
                del self._overrides[matched.id]

        verdict = matched.action
        if self._audit:
            self._audit.log_override_hit(
                override_id=matched.id,
                pattern=matched.pattern,
                tool_name=tool_name,
                verdict=verdict.value,
                use_count=matched.use_count,
            )

        return ActionDecision(
            verdict=verdict,
            tool_name=tool_name,
            matched_rule=f"temp_override:{matched.pattern}",
            reason=f"Temporary {verdict.value} override (pattern={matched.pattern!r}, "
            f"uses={matched.use_count}/{matched.max_uses or '∞'})",
        )
