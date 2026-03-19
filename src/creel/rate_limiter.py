"""LLM call rate limiting — token bucket, rolling windows, and cost caps.

Prevents runaway costs from agent loops or misconfigured tasks by enforcing
configurable per-minute, per-hour, daily token, and daily cost limits.
"""

from __future__ import annotations

import fcntl
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Model pricing per 1M tokens (USD) — input / output.
# Update this table when new models are released; unknown models fall back to
# _DEFAULT_PRICING below.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Claude 4 family (current aliases)
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-opus-4-6": (15.00, 75.00),
    # Claude 4 family (dated IDs — keep until fully deprecated)
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-opus-4-20250514": (15.00, 75.00),
    # Claude 3.5 family
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    # Claude 3 family
    "claude-3-opus-20240229": (15.00, 75.00),
    "claude-3-sonnet-20240229": (3.00, 15.00),
    "claude-3-haiku-20240307": (0.25, 1.25),
}

# Default pricing for unknown models
_DEFAULT_PRICING = (3.00, 15.00)

# Alert thresholds
WARN_THRESHOLD = 0.80
LIMIT_THRESHOLD = 1.00


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a single LLM call."""
    input_rate, output_rate = MODEL_PRICING.get(model, _DEFAULT_PRICING)
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


@dataclass
class _TimestampedRequest:
    """A recorded request with its timestamp and token/cost data."""

    timestamp: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class UsageSnapshot:
    """Current usage statistics for display."""

    requests_last_minute: int = 0
    requests_last_hour: int = 0
    tokens_today: int = 0
    cost_today_usd: float = 0.0
    requests_per_minute_limit: int = 0
    requests_per_hour_limit: int = 0
    tokens_per_day_limit: int = 0
    cost_per_day_limit_usd: float = 0.0
    override_active: bool = False
    override_expires_at: float | None = None


class RateLimitExceeded(Exception):
    """Raised when a rate limit is hit and the request cannot proceed."""

    def __init__(self, limit_type: str, current: float, limit: float, retry_after: float = 0.0):
        self.limit_type = limit_type
        self.current = current
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded: {limit_type} "
            f"(current={current:.1f}, limit={limit:.1f}, retry_after={retry_after:.1f}s)"
        )


class RateLimiter:
    """Rate limiter for LLM API calls.

    Implements:
    - Token bucket for per-minute request rate limiting
    - Rolling window for per-hour request limiting
    - Daily rolling window for token and cost caps
    - Alert callbacks at 80% and 100% thresholds
    - Emergency override mechanism
    """

    def __init__(
        self,
        requests_per_minute: int = 30,
        requests_per_hour: int = 500,
        tokens_per_day: int = 1_000_000,
        cost_per_day_usd: float = 10.00,
        queue_timeout: float = 30.0,
        on_alert: Callable | None = None,
        usage_dir: Path | str | None = None,
    ):
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.tokens_per_day = tokens_per_day
        self.cost_per_day_usd = cost_per_day_usd
        self.queue_timeout = queue_timeout
        self._on_alert = on_alert

        # Token bucket state for per-minute limiting
        self._bucket_tokens = float(requests_per_minute)
        self._bucket_max = float(requests_per_minute)
        self._bucket_last_refill = time.monotonic()
        self._bucket_rate = requests_per_minute / 60.0  # tokens per second

        # Rolling window storage
        self._requests: list[_TimestampedRequest] = []
        self._lock = threading.Lock()

        # Override state
        self._override_until: float = 0.0

        # Persistent usage directory
        self._usage_dir: Path | None = None
        if usage_dir is not None:
            self._usage_dir = Path(usage_dir)
            self._usage_dir.mkdir(parents=True, exist_ok=True)

        # Track which alerts have been fired to avoid spam
        self._alerts_fired: set[str] = set()

    # --- Public API ---

    def check(self, block: bool = True) -> None:
        """Check whether a new request is allowed.

        If *block* is True (default), waits up to *queue_timeout* seconds for
        the token bucket to refill.  If False, raises immediately.

        Raises RateLimitExceeded if the request cannot proceed.
        """
        if self._is_override_active():
            return

        # Acquire the bucket token first (may block), then re-verify daily/hourly
        # limits under the lock to close the TOCTOU gap between limit check and
        # token acquisition.
        self._acquire_bucket_token(block=block)

        with self._lock:
            self._prune_old_requests()
            self._check_daily_limits()
            self._check_hourly_limit()

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """Record a completed LLM call for tracking."""
        cost = estimate_cost(model, input_tokens, output_tokens)
        now = time.time()

        with self._lock:
            self._requests.append(
                _TimestampedRequest(
                    timestamp=now,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                )
            )

        self._check_alerts()
        self._persist_record(now, model, input_tokens, output_tokens, cost)

    def get_usage(self) -> UsageSnapshot:
        """Return a snapshot of current usage."""
        now = time.time()
        with self._lock:
            self._prune_old_requests()
            minute_ago = now - 60
            hour_ago = now - 3600
            day_ago = now - 86400

            rpm = sum(1 for r in self._requests if r.timestamp >= minute_ago)
            rph = sum(1 for r in self._requests if r.timestamp >= hour_ago)
            tokens_day = sum(
                r.input_tokens + r.output_tokens for r in self._requests if r.timestamp >= day_ago
            )
            cost_day = sum(r.cost_usd for r in self._requests if r.timestamp >= day_ago)

        return UsageSnapshot(
            requests_last_minute=rpm,
            requests_last_hour=rph,
            tokens_today=tokens_day,
            cost_today_usd=cost_day,
            requests_per_minute_limit=self.requests_per_minute,
            requests_per_hour_limit=self.requests_per_hour,
            tokens_per_day_limit=self.tokens_per_day,
            cost_per_day_limit_usd=self.cost_per_day_usd,
            override_active=self._is_override_active(),
            override_expires_at=self._override_until if self._is_override_active() else None,
        )

    def override(self, duration_seconds: float) -> None:
        """Temporarily disable rate limits for *duration_seconds*."""
        self._override_until = time.time() + duration_seconds
        logger.warning("Rate limit override activated for %.0f seconds", duration_seconds)

    def get_usage_history(self, days: int = 7) -> list[dict]:
        """Return daily usage breakdown for the last *days* days.

        Reads from persisted usage files if available, otherwise uses
        in-memory data.
        """
        if self._usage_dir is not None:
            return self._load_persisted_history(days)

        # Fall back to in-memory data
        return self._compute_history_from_memory(days)

    # --- Internal helpers ---

    def _is_override_active(self) -> bool:
        return time.time() < self._override_until

    def _acquire_bucket_token(self, block: bool) -> None:
        """Acquire a token from the per-minute bucket.

        Uses time.monotonic() for interval timing (immune to wall-clock jumps),
        while rolling-window checks use time.time() for calendar-based windows.
        """
        deadline = time.monotonic() + self.queue_timeout if block else time.monotonic()
        logged_block = False

        while True:
            now = time.monotonic()
            with self._lock:
                # Refill bucket
                elapsed = now - self._bucket_last_refill
                self._bucket_tokens = min(
                    self._bucket_max,
                    self._bucket_tokens + elapsed * self._bucket_rate,
                )
                self._bucket_last_refill = now

                if self._bucket_tokens >= 1.0:
                    self._bucket_tokens -= 1.0
                    return

            if now >= deadline:
                raise RateLimitExceeded(
                    limit_type="requests_per_minute",
                    current=float(self.requests_per_minute),
                    limit=float(self.requests_per_minute),
                    retry_after=1.0 / self._bucket_rate if self._bucket_rate > 0 else 60.0,
                )

            if not logged_block:
                logger.info(
                    "Rate limiter: waiting for bucket token (timeout=%.1fs)", self.queue_timeout
                )
                logged_block = True

            # Wait a short interval before retrying
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _prune_old_requests(self) -> None:
        """Remove requests older than 24 hours (must hold lock)."""
        cutoff = time.time() - 86400
        self._requests = [r for r in self._requests if r.timestamp >= cutoff]

    def _check_hourly_limit(self) -> None:
        """Check per-hour request limit (must hold lock)."""
        hour_ago = time.time() - 3600
        count = sum(1 for r in self._requests if r.timestamp >= hour_ago)
        if count >= self.requests_per_hour:
            oldest_in_window = min(
                (r.timestamp for r in self._requests if r.timestamp >= hour_ago),
                default=time.time(),
            )
            retry_after = oldest_in_window + 3600 - time.time()
            raise RateLimitExceeded(
                limit_type="requests_per_hour",
                current=float(count),
                limit=float(self.requests_per_hour),
                retry_after=max(0, retry_after),
            )

    def _check_daily_limits(self) -> None:
        """Check daily token and cost limits (must hold lock)."""
        day_ago = time.time() - 86400
        day_requests = [r for r in self._requests if r.timestamp >= day_ago]

        total_tokens = sum(r.input_tokens + r.output_tokens for r in day_requests)
        if total_tokens >= self.tokens_per_day:
            raise RateLimitExceeded(
                limit_type="tokens_per_day",
                current=float(total_tokens),
                limit=float(self.tokens_per_day),
            )

        total_cost = sum(r.cost_usd for r in day_requests)
        if total_cost >= self.cost_per_day_usd:
            raise RateLimitExceeded(
                limit_type="cost_per_day_usd",
                current=total_cost,
                limit=self.cost_per_day_usd,
            )

    def _check_alerts(self) -> None:
        """Fire alerts when approaching or hitting limits."""
        usage = self.get_usage()

        checks = [
            (
                "requests_per_minute",
                usage.requests_last_minute,
                usage.requests_per_minute_limit,
            ),
            (
                "requests_per_hour",
                usage.requests_last_hour,
                usage.requests_per_hour_limit,
            ),
            ("tokens_per_day", usage.tokens_today, usage.tokens_per_day_limit),
            ("cost_per_day_usd", usage.cost_today_usd, usage.cost_per_day_limit_usd),
        ]

        for name, current, limit in checks:
            if limit <= 0:
                continue
            ratio = current / limit

            warn_key = f"{name}_warn"
            limit_key = f"{name}_limit"

            if ratio >= LIMIT_THRESHOLD and limit_key not in self._alerts_fired:
                self._alerts_fired.add(limit_key)
                self._fire_alert("limit_hit", name, current, limit)
            elif ratio >= WARN_THRESHOLD and warn_key not in self._alerts_fired:
                self._alerts_fired.add(warn_key)
                self._fire_alert("approaching_limit", name, current, limit)

    def _fire_alert(self, level: str, limit_type: str, current: float, limit: float) -> None:
        pct = (current / limit * 100) if limit > 0 else 0
        msg = f"Rate limit {level}: {limit_type} at {pct:.0f}% ({current:.1f}/{limit:.1f})"
        if level == "limit_hit":
            logger.warning(msg)
        else:
            logger.info(msg)

        if self._on_alert is not None:
            self._on_alert(level, limit_type, current, limit)

    def _persist_record(
        self,
        timestamp: float,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        """Append a usage record to a daily JSON-lines file."""
        if self._usage_dir is None:
            return
        try:
            import datetime

            dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.UTC)
            filename = dt.strftime("%Y-%m-%d") + ".jsonl"
            path = self._usage_dir / filename
            record = {
                "ts": timestamp,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost_usd, 6),
            }
            with open(path, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(json.dumps(record) + "\n")
        except Exception:
            logger.debug("Failed to persist usage record", exc_info=True)

    @staticmethod
    def _empty_day(date_str: str) -> dict:
        return {
            "date": date_str,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }

    def _load_persisted_history(self, days: int) -> list[dict]:
        """Load daily summaries from persisted JSONL files."""
        import datetime

        result = []
        today = datetime.datetime.now(datetime.UTC).date()
        assert self._usage_dir is not None
        for i in range(days):
            day = today - datetime.timedelta(days=i)
            filename = day.strftime("%Y-%m-%d") + ".jsonl"
            path = self._usage_dir / filename
            if not path.exists():
                result.append(self._empty_day(day.isoformat()))
                continue
            try:
                requests = 0
                input_tok = 0
                output_tok = 0
                cost = 0.0
                for line in path.read_text().splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    requests += 1
                    input_tok += rec.get("input_tokens", 0)
                    output_tok += rec.get("output_tokens", 0)
                    cost += rec.get("cost_usd", 0.0)
                result.append(
                    {
                        "date": day.isoformat(),
                        "requests": requests,
                        "input_tokens": input_tok,
                        "output_tokens": output_tok,
                        "total_tokens": input_tok + output_tok,
                        "cost_usd": round(cost, 4),
                    }
                )
            except Exception:
                logger.debug("Failed to read usage file %s", path, exc_info=True)
                result.append(self._empty_day(day.isoformat()))
        return result

    def _compute_history_from_memory(self, days: int) -> list[dict]:
        """Compute daily summaries from in-memory request data."""
        import datetime

        today = datetime.datetime.now(datetime.UTC).date()
        result = []
        for i in range(days):
            day = today - datetime.timedelta(days=i)
            day_start = datetime.datetime.combine(
                day, datetime.time.min, tzinfo=datetime.UTC
            ).timestamp()
            day_end = day_start + 86400

            with self._lock:
                day_requests = [r for r in self._requests if day_start <= r.timestamp < day_end]

            input_tok = sum(r.input_tokens for r in day_requests)
            output_tok = sum(r.output_tokens for r in day_requests)
            cost = sum(r.cost_usd for r in day_requests)
            result.append(
                {
                    "date": day.isoformat(),
                    "requests": len(day_requests),
                    "input_tokens": input_tok,
                    "output_tokens": output_tok,
                    "total_tokens": input_tok + output_tok,
                    "cost_usd": round(cost, 4),
                }
            )
        return result


# --- Module-level singleton ---

_global_limiter: RateLimiter | None = None
_global_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter | None:
    """Return the global rate limiter, or None if not configured."""
    return _global_limiter


def configure_rate_limiter(
    requests_per_minute: int = 30,
    requests_per_hour: int = 500,
    tokens_per_day: int = 1_000_000,
    cost_per_day_usd: float = 10.00,
    queue_timeout: float = 30.0,
    on_alert: Callable | None = None,
    usage_dir: Path | str | None = None,
) -> RateLimiter:
    """Create and install the global rate limiter."""
    global _global_limiter
    with _global_lock:
        _global_limiter = RateLimiter(
            requests_per_minute=requests_per_minute,
            requests_per_hour=requests_per_hour,
            tokens_per_day=tokens_per_day,
            cost_per_day_usd=cost_per_day_usd,
            queue_timeout=queue_timeout,
            on_alert=on_alert,
            usage_dir=usage_dir,
        )
    return _global_limiter


def reset_global_limiter() -> None:
    """Clear the global rate limiter (mainly for tests)."""
    global _global_limiter
    with _global_lock:
        _global_limiter = None
