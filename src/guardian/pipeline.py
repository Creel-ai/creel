"""Guardian pipeline — async parallel/sequential check executor.

Runs independent Guardian checks concurrently via ``asyncio``, with support
for short-circuiting on first block, dependency-aware sequential phases,
and a configurable pipeline-level timeout.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from guardian.types import ActionVerdict

if TYPE_CHECKING:
    from guardian.core import Guardian
    from guardian.types import PipelineConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Result from a single pipeline check."""

    name: str
    blocked: bool
    result: Any = None
    duration_ms: float = 0.0
    error: str | None = None


@dataclass
class PipelineContext:
    """Input bag for pipeline checks — each check reads only what it needs."""

    text: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    user_request: str = ""
    prior_tools: list[str] | None = None
    available_tools: list[str] | None = None
    tool_output: str = ""
    output_length: int = 0
    success: bool = True


@dataclass
class PipelineResult:
    """Aggregate result from running the full pipeline."""

    blocked: bool
    results: dict[str, CheckResult] = field(default_factory=dict)
    short_circuited: bool = False
    timed_out: bool = False
    total_duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Pipeline executor
# ---------------------------------------------------------------------------


# Type alias for an async check function — using a Protocol instead of Any
# so that static analysis can verify check signatures.
class CheckFn(Protocol):
    async def __call__(self, ctx: PipelineContext) -> CheckResult: ...


class GuardianPipeline:
    """Async executor for the Guardian safety-check pipeline.

    Usage::

        pipeline = GuardianPipeline(guardian, config.pipeline)
        result = await pipeline.run(PipelineContext(text="user input", ...))
        if result.blocked:
            ...
    """

    def __init__(self, guardian: Guardian, config: PipelineConfig) -> None:
        self._guardian = guardian
        self._config = config
        self._checks: dict[str, CheckFn] = self._build_check_registry()

    # -- check registry -----------------------------------------------------

    def _build_check_registry(self) -> dict[str, CheckFn]:
        return {
            "injection_detector": self._check_injection,
            "policy_engine": self._check_policy,
            "coherence_checker": self._check_coherence,
            "credential_scanner": self._check_credentials,
            "drift_detector": self._check_drift,
        }

    # -- individual check wrappers ------------------------------------------
    # NOTE: check wrappers use ``asyncio.to_thread`` to run synchronous
    # Guardian methods without blocking the event loop.  When the pipeline
    # runs parallel checks, multiple threads may invoke Guardian (and its
    # AuditLogger) concurrently.  Single-line JSONL appends are atomic on
    # POSIX, but the size-based rotation path in AuditLogger is not
    # thread-safe.  If audit rotation is enabled alongside parallel checks,
    # consider adding a lock in AuditLogger._write.

    async def _check_injection(self, ctx: PipelineContext) -> CheckResult:
        start = time.monotonic()
        result = await asyncio.to_thread(self._guardian.screen_input, ctx.text)
        elapsed = (time.monotonic() - start) * 1000
        return CheckResult(
            name="injection_detector",
            blocked=result.blocked,
            result=result,
            duration_ms=elapsed,
        )

    async def _check_policy(self, ctx: PipelineContext) -> CheckResult:
        """Check tool action against the policy engine.

        Only DENY verdicts set ``blocked=True``.  REVIEW verdicts are
        available in ``CheckResult.result`` (an ``ActionDecision``) so
        callers can implement their own approval flow.
        """
        start = time.monotonic()
        result = await asyncio.to_thread(
            self._guardian.validate_action, ctx.tool_name, ctx.tool_args
        )
        elapsed = (time.monotonic() - start) * 1000
        return CheckResult(
            name="policy_engine",
            blocked=result.verdict == ActionVerdict.DENY,
            result=result,
            duration_ms=elapsed,
        )

    async def _check_coherence(self, ctx: PipelineContext) -> CheckResult:
        start = time.monotonic()
        result = await asyncio.to_thread(
            self._guardian.check_coherence,
            ctx.user_request,
            ctx.tool_name,
            ctx.tool_args,
            ctx.prior_tools,
            ctx.available_tools,
        )
        elapsed = (time.monotonic() - start) * 1000
        return CheckResult(
            name="coherence_checker",
            blocked=not result.coherent,
            result=result,
            duration_ms=elapsed,
        )

    async def _check_credentials(self, ctx: PipelineContext) -> CheckResult:
        start = time.monotonic()
        result = await asyncio.to_thread(
            self._guardian.scan_tool_output_credentials,
            ctx.tool_name,
            ctx.tool_output,
        )
        elapsed = (time.monotonic() - start) * 1000
        return CheckResult(
            name="credential_scanner",
            blocked=bool(result),
            result=result,
            duration_ms=elapsed,
        )

    async def _check_drift(self, ctx: PipelineContext) -> CheckResult:
        start = time.monotonic()
        result = await asyncio.to_thread(
            self._guardian.check_drift,
            ctx.tool_name,
            ctx.output_length,
            ctx.success,
        )
        elapsed = (time.monotonic() - start) * 1000
        return CheckResult(
            name="drift_detector",
            blocked=False,  # drift alerts are warnings, not blocks
            result=result,
            duration_ms=elapsed,
        )

    # -- pipeline execution -------------------------------------------------

    async def run(self, context: PipelineContext) -> PipelineResult:
        """Execute the full pipeline: parallel checks → sequential checks."""
        start = time.monotonic()
        results: dict[str, CheckResult] = {}
        timed_out = False

        try:
            coro = self._run_phases(context, results)
            if self._config.timeout > 0:
                await asyncio.wait_for(coro, timeout=self._config.timeout)
            else:
                await coro
        except TimeoutError:
            timed_out = True
            logger.warning("Guardian pipeline timed out after %.1fs", self._config.timeout)

        blocked = timed_out or any(cr.blocked for cr in results.values())

        # Determine if we short-circuited (blocked before all checks ran)
        all_checks = set(self._config.parallel_checks + self._config.sequential_checks)
        configured = {n for n in all_checks if n in self._checks}
        short_circuited = blocked and set(results.keys()) < configured

        total_ms = (time.monotonic() - start) * 1000
        return PipelineResult(
            blocked=blocked,
            results=results,
            short_circuited=short_circuited,
            timed_out=timed_out,
            total_duration_ms=total_ms,
        )

    async def _run_phases(
        self,
        context: PipelineContext,
        results: dict[str, CheckResult],
    ) -> None:
        """Run parallel phase, then sequential phase."""
        # Phase 1: parallel checks
        if self._config.parallel_checks:
            await self._run_parallel(context, results)

        # Short-circuit: skip sequential phase if already blocked
        if self._config.short_circuit and any(r.blocked for r in results.values()):
            return

        # Phase 2: sequential checks
        if self._config.sequential_checks:
            await self._run_sequential(context, results)

    async def _run_parallel(
        self,
        context: PipelineContext,
        results: dict[str, CheckResult],
    ) -> None:
        """Run parallel checks concurrently."""
        check_names = [name for name in self._config.parallel_checks if name in self._checks]
        if not check_names:
            return

        if self._config.short_circuit:
            await self._run_parallel_short_circuit(check_names, context, results)
        else:
            tasks = [self._checks[name](context) for name in check_names]
            completed = await asyncio.gather(*tasks, return_exceptions=True)
            for name, outcome in zip(check_names, completed, strict=True):
                if isinstance(outcome, BaseException):
                    logger.error("Check %s failed: %s", name, outcome)
                    results[name] = CheckResult(
                        name=name, blocked=False, result=None, error=str(outcome)
                    )
                else:
                    results[name] = outcome

    async def _run_parallel_short_circuit(
        self,
        check_names: list[str],
        context: PipelineContext,
        results: dict[str, CheckResult],
    ) -> None:
        """Run checks concurrently, cancelling remaining on first block."""
        pending: dict[asyncio.Task[CheckResult], str] = {}
        for name in check_names:
            task = asyncio.create_task(self._checks[name](context))
            pending[task] = name

        try:
            while pending:
                done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    name = pending.pop(task)
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc:
                        logger.error("Check %s failed: %s", name, exc)
                        results[name] = CheckResult(
                            name=name, blocked=False, result=None, error=str(exc)
                        )
                    else:
                        cr = task.result()
                        results[name] = cr
                        if cr.blocked:
                            # Cancel all remaining tasks
                            for t in pending:
                                t.cancel()
                            if pending:
                                await asyncio.gather(*pending.keys(), return_exceptions=True)
                            return
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()

    async def _run_sequential(
        self,
        context: PipelineContext,
        results: dict[str, CheckResult],
    ) -> None:
        """Run sequential checks one at a time."""
        for name in self._config.sequential_checks:
            if name not in self._checks:
                continue
            try:
                cr = await self._checks[name](context)
                results[name] = cr
                if cr.blocked and self._config.short_circuit:
                    return
            except Exception as exc:
                logger.error("Check %s failed: %s", name, exc)
                results[name] = CheckResult(name=name, blocked=False, result=None, error=str(exc))
