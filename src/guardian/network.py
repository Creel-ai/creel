"""Network traffic monitor — domain filtering, size limits, rate limiting, and audit."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

from guardian.types import NetworkPolicyConfig

logger = logging.getLogger(__name__)

_MAX_REQUEST_LOG_SIZE = 10_000


@dataclass
class NetworkVerdict:
    """Result of checking a request against the network policy."""

    allowed: bool
    reason: str = ""
    domain: str = ""
    is_unknown_domain: bool = False


@dataclass
class _RateBucket:
    """Sliding-window rate counter for a single executor."""

    timestamps: list[float] = field(default_factory=list)


class NetworkMonitor:
    """Application-level network traffic monitor.

    Enforces domain allowlist/blocklist, request/response size limits,
    and per-executor rate limiting.  All requests are logged to the
    guardian audit log for review.
    """

    def __init__(self, config: NetworkPolicyConfig) -> None:
        self._config = config
        self._rate_buckets: dict[str, _RateBucket] = defaultdict(_RateBucket)
        self._request_log: deque[dict] = deque(maxlen=_MAX_REQUEST_LOG_SIZE)

    @property
    def config(self) -> NetworkPolicyConfig:
        return self._config

    @property
    def request_log(self) -> list[dict]:
        """Return a copy of the in-memory request log."""
        return list(self._request_log)

    def check_domain(self, url: str) -> NetworkVerdict:
        """Check whether a URL's domain is allowed by the network policy.

        Evaluation order:
        1. Blocked domains — if matched, deny immediately.
        2. Allowed domains — if the list is non-empty and matched, allow.
        3. If allowed list is non-empty and *not* matched, deny (unknown).
        4. If allowed list is empty, allow (no allowlist = permissive).
        """
        domain = _extract_domain(url)
        if not domain:
            return NetworkVerdict(allowed=False, reason="invalid URL", domain="")

        # Check blocklist first
        for pattern in self._config.blocked_domains:
            if _domain_matches(domain, pattern):
                return NetworkVerdict(
                    allowed=False,
                    reason=f"domain '{domain}' matches blocked pattern '{pattern}'",
                    domain=domain,
                )

        # Check allowlist
        if self._config.allowed_domains:
            for pattern in self._config.allowed_domains:
                if _domain_matches(domain, pattern):
                    return NetworkVerdict(allowed=True, domain=domain)
            # Not in allowlist
            return NetworkVerdict(
                allowed=False,
                reason=f"domain '{domain}' not in allowed domains",
                domain=domain,
                is_unknown_domain=True,
            )

        # No allowlist configured — permissive mode
        return NetworkVerdict(allowed=True, domain=domain)

    def check_request_size(self, size_bytes: int) -> NetworkVerdict:
        """Check whether a request body exceeds the configured limit."""
        limit = int(self._config.max_request_size_mb * 1024 * 1024)
        if size_bytes > limit:
            return NetworkVerdict(
                allowed=False,
                reason=(
                    f"request size {size_bytes} bytes exceeds "
                    f"limit of {self._config.max_request_size_mb} MB"
                ),
            )
        return NetworkVerdict(allowed=True)

    def check_response_size(self, size_bytes: int) -> NetworkVerdict:
        """Check whether a response body exceeds the configured limit."""
        limit = int(self._config.max_response_size_mb * 1024 * 1024)
        if size_bytes > limit:
            return NetworkVerdict(
                allowed=False,
                reason=(
                    f"response size {size_bytes} bytes exceeds "
                    f"limit of {self._config.max_response_size_mb} MB"
                ),
            )
        return NetworkVerdict(allowed=True)

    def consume_rate_slot(self, executor: str) -> NetworkVerdict:
        """Check per-executor rate limiting (sliding 60-second window)."""
        now = time.monotonic()
        bucket = self._rate_buckets[executor]
        window_start = now - 60.0

        # Prune old entries
        bucket.timestamps = [t for t in bucket.timestamps if t > window_start]

        if len(bucket.timestamps) >= self._config.rate_limit_per_minute:
            return NetworkVerdict(
                allowed=False,
                reason=(
                    f"executor '{executor}' exceeded rate limit of "
                    f"{self._config.rate_limit_per_minute} requests/minute"
                ),
            )

        bucket.timestamps.append(now)
        return NetworkVerdict(allowed=True)

    def check_request(
        self,
        url: str,
        *,
        executor: str = "",
        method: str = "GET",
        request_size_bytes: int = 0,
    ) -> NetworkVerdict:
        """Run all pre-request checks and return a combined verdict.

        Checks domain, request size, and rate limit in order.
        """
        # Domain check
        domain_verdict = self.check_domain(url)
        if not domain_verdict.allowed:
            self._log_request(
                url=url,
                domain=domain_verdict.domain,
                executor=executor,
                method=method,
                request_size_bytes=request_size_bytes,
                blocked=True,
                block_reason=domain_verdict.reason,
            )
            return domain_verdict

        # Request size check
        size_verdict = self.check_request_size(request_size_bytes)
        if not size_verdict.allowed:
            self._log_request(
                url=url,
                domain=domain_verdict.domain,
                executor=executor,
                method=method,
                request_size_bytes=request_size_bytes,
                blocked=True,
                block_reason=size_verdict.reason,
            )
            return size_verdict

        # Rate limit check
        rate_verdict = self.consume_rate_slot(executor)
        if not rate_verdict.allowed:
            self._log_request(
                url=url,
                domain=domain_verdict.domain,
                executor=executor,
                method=method,
                request_size_bytes=request_size_bytes,
                blocked=True,
                block_reason=rate_verdict.reason,
            )
            return rate_verdict

        return NetworkVerdict(allowed=True, domain=domain_verdict.domain)

    def record_response(
        self,
        url: str,
        *,
        executor: str = "",
        method: str = "GET",
        request_size_bytes: int = 0,
        response_size_bytes: int = 0,
        status_code: int | None = None,
    ) -> NetworkVerdict | None:
        """Record a completed response and check response size.

        Returns a NetworkVerdict only if the response size exceeds the limit
        (as an alert — the response has already been received).
        """
        verdict = self.check_response_size(response_size_bytes)
        domain = _extract_domain(url)

        self._log_request(
            url=url,
            domain=domain,
            executor=executor,
            method=method,
            request_size_bytes=request_size_bytes,
            response_size_bytes=response_size_bytes,
            status_code=status_code,
            blocked=False,
            block_reason="",
        )

        if not verdict.allowed:
            logger.warning(
                "Network alert: %s (executor=%s, url=%s)",
                verdict.reason,
                executor,
                _sanitize_url(url),
            )
            return verdict
        return None

    def _log_request(
        self,
        *,
        url: str,
        domain: str,
        executor: str,
        method: str,
        request_size_bytes: int = 0,
        response_size_bytes: int = 0,
        status_code: int | None = None,
        blocked: bool = False,
        block_reason: str = "",
    ) -> None:
        """Append a request record to the in-memory log.

        Sanitizes the URL (strips query string and fragment) to avoid
        storing credentials from query parameters.
        """
        sanitized = _sanitize_url(url)
        record = {
            "url": sanitized,
            "domain": domain,
            "executor": executor,
            "method": method,
            "request_size_bytes": request_size_bytes,
            "response_size_bytes": response_size_bytes,
            "status_code": status_code,
            "blocked": blocked,
            "block_reason": block_reason,
            "ts": time.time(),
        }
        self._request_log.append(record)

        if blocked:
            logger.warning(
                "Network request blocked: %s (executor=%s, reason=%s)",
                sanitized,
                executor,
                block_reason,
            )


def _sanitize_url(url: str) -> str:
    """Strip query string and fragment from a URL to avoid logging sensitive params."""
    from urllib.parse import urlunparse

    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _extract_domain(url: str) -> str:
    """Extract the hostname from a URL.

    Rejects URLs with userinfo (user@host) to prevent URL parser
    differential attacks where the domain check passes for the
    hostname portion but an HTTP client connects to the userinfo host.
    """
    parsed = urlparse(url)
    # Reject URLs with userinfo — potential parser confusion attack
    if "@" in (parsed.netloc or ""):
        logger.warning("Rejecting URL with userinfo component: %s", _sanitize_url(url))
        return ""
    return parsed.hostname or ""


def _domain_matches(domain: str, pattern: str) -> bool:
    """Check if a domain matches a pattern with proper subdomain boundaries.

    Supports ``*.googleapis.com`` (any subdomain of googleapis.com)
    and exact matches like ``api.openai.com``.  Unlike fnmatch, the
    wildcard prefix enforces a dot boundary so ``evilgoogleapis.com``
    does NOT match ``*.googleapis.com``.
    """
    if pattern.startswith("*."):
        suffix = pattern[1:]  # e.g. ".googleapis.com"
        return domain.endswith(suffix) and domain != suffix[1:]
    return domain == pattern
