"""Guardian core — orchestrates the multi-stage security pipeline."""

from __future__ import annotations

import logging

from guardian.audit import AuditLogger, _hash_text
from guardian.coherence import CoherenceChecker
from guardian.credential_scanner import CredentialMatch, scan_for_credentials
from guardian.drift import DriftDetector
from guardian.fast_classifier import FastClassifier
from guardian.llm_judge import LLMJudge
from guardian.network import NetworkMonitor, NetworkVerdict, _extract_domain
from guardian.pipeline import GuardianPipeline, PipelineContext, PipelineResult
from guardian.policy import PolicyEngine
from guardian.types import (
    ActionDecision,
    ActionVerdict,
    CoherenceResult,
    GuardianConfig,
    ScreenResult,
)

logger = logging.getLogger(__name__)


class Guardian:
    """Orchestrates the multi-stage security pipeline.

    Stages:
        1. Fast classifier (DeBERTa/ONNX) — local, ~10ms
        2. LLM judge (Haiku) — optional, ~300ms
        3. Policy engine (YAML rules) — <1ms

    If not configured or ``enabled=False``, all methods pass through.
    """

    def __init__(self, config: GuardianConfig) -> None:
        from creel import paths

        self._config = config
        self._classifier = FastClassifier(config.fast_classifier)
        self._judge = LLMJudge(config.llm_judge)
        policy_file = config.policy.policy_file or str(paths.policies_dir() / "default.yaml")
        self._policy = PolicyEngine(policy_file) if config.policy.enabled else None
        self._coherence = CoherenceChecker(config.coherence)

        audit_file = config.audit.log_file or str(paths.audit_log())
        self._audit = (
            AuditLogger(
                audit_file,
                rotate_daily=config.audit.rotate_daily,
                max_size_mb=config.audit.max_size_mb,
            )
            if config.audit.enabled
            else None
        )
        self._drift = (
            DriftDetector(
                z_threshold=config.drift.z_threshold,
                error_threshold=config.drift.error_threshold,
                error_window_size=config.drift.error_window_size,
                new_tool_grace_count=config.drift.new_tool_grace_count,
                audit_log_path=audit_file if config.audit.enabled else None,
            )
            if config.drift.enabled
            else None
        )
        self._network = (
            NetworkMonitor(config.network_policy) if config.network_policy.enabled else None
        )
        self._pipeline = GuardianPipeline(self, config.pipeline)

        logger.info(
            "Guardian initialized (classifier=%s, judge=%s, policy=%s, drift=%s, network=%s)",
            config.fast_classifier.enabled,
            config.llm_judge.enabled,
            config.policy.enabled,
            config.drift.enabled,
            config.network_policy.enabled,
        )

    def warm_up(self) -> None:
        """Eagerly warm up the fast classifier at startup."""
        self._classifier.warm_up()

    @property
    def judge_usage(self) -> dict:
        """Return cumulative LLM judge usage stats."""
        return self._judge.usage_stats

    def screen_input(self, text: str) -> ScreenResult:
        """Screen incoming text for prompt injection (stages 1+2).

        Returns a ScreenResult indicating whether the input was blocked.
        """
        # Stage 1: fast classifier
        chunk_details: list[dict] = []
        if self._config.debug:
            classifier_result, chunk_details = self._classifier.classify_detailed(text)
        else:
            classifier_result = self._classifier.classify(text)

        blocked = False
        if classifier_result and classifier_result.is_injection:
            blocked = True
            logger.warning(
                "Fast classifier blocked input (confidence=%.3f)",
                classifier_result.confidence,
            )

        # Stage 2: LLM judge — conditional on classifier uncertainty
        classifier_confidence = classifier_result.confidence if classifier_result else None
        judge_result = None
        if not blocked and self._judge.should_run(classifier_confidence):
            judge_result = self._judge.judge(text)
            if judge_result and judge_result.is_injection:
                blocked = True
                logger.warning(
                    "LLM judge blocked input (confidence=%.3f): %s",
                    judge_result.confidence,
                    judge_result.reasoning,
                )

        # Audit
        if self._audit:
            source = "none"
            confidence = None
            if blocked and classifier_result and classifier_result.is_injection:
                source = "fast_classifier"
                confidence = classifier_result.confidence
            elif blocked and judge_result and judge_result.is_injection:
                source = "llm_judge"
                confidence = judge_result.confidence
            elif classifier_result:
                source = "fast_classifier"
                confidence = classifier_result.confidence

            self._audit.log_screen(
                input_hash=_hash_text(text),
                input_length=len(text),
                blocked=blocked,
                source=source,
                confidence=confidence,
            )

            if self._config.debug and chunk_details:
                self._audit.log_screen_debug(
                    text=text,
                    chunks=chunk_details,
                    blocked=blocked,
                    source=source,
                )

        rejection_message = ""
        if blocked:
            rejection_message = "I can't process that request. Please rephrase your message."

        return ScreenResult(
            blocked=blocked,
            classifier_result=classifier_result,
            judge_result=judge_result,
            rejection_message=rejection_message,
        )

    @staticmethod
    def _strip_html(text: str) -> str:
        """Strip HTML tags and decode entities for cleaner classification.

        The DeBERTa classifier is trained on natural language, not HTML markup.
        Raw HTML causes high false-positive rates on benign web content.

        Note: This is a best-effort heuristic to reduce noise, not a security
        boundary. The classifier and judge remain the actual detection layers.
        """
        import html
        import re

        # Remove script/style blocks entirely
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Decode HTML entities
        text = html.unescape(text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _extract_page_text(text: str) -> str:
        """Extract plain text from tool output for classification.

        Tool output (especially browser tools) may be JSON containing
        accessibility tree nodes, HTML, or other structured formats.
        The DeBERTa classifier works on natural language, so we extract
        just the human-readable text content.
        """
        import json

        # Try to parse as JSON and extract text fields from a11y tree nodes
        try:
            data = json.loads(text)

            # Handle {"content": [...nodes...]} or {"result": {..., "content": [...]}}
            nodes = None
            if isinstance(data, dict):
                nodes = data.get("content") or data.get("nodes")
                if nodes is None and "result" in data:
                    inner = data["result"]
                    if isinstance(inner, dict):
                        nodes = inner.get("content") or inner.get("nodes")
            elif isinstance(data, list):
                nodes = data

            if isinstance(nodes, list):
                # Extract name and value fields — skip structural keys
                # like role, level, depth
                texts = []
                for node in nodes:
                    if isinstance(node, dict):
                        for key in ("name", "value", "text"):
                            val = node.get(key)
                            if val and isinstance(val, str):
                                texts.append(val)
                if texts:
                    return " ".join(texts)

            # If it's JSON but not a node list, just stringify values
            if isinstance(data, dict):
                parts = []
                for v in data.values():
                    if isinstance(v, str):
                        parts.append(v)
                if parts:
                    return " ".join(parts)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        # Fall back to HTML stripping for non-JSON content
        return Guardian._strip_html(text)

    def screen_tool_result(self, tool_name: str, text: str) -> ScreenResult:
        """Screen a tool result for prompt injection and log details.

        Extracts plain text from structured tool output (JSON a11y trees,
        HTML, etc.) before classification so the DeBERTa model sees natural
        language instead of structural markup that triggers false positives.
        """
        cleaned = self._extract_page_text(text)
        result = self.screen_input(cleaned)

        if result.blocked and self._audit:
            blocker = result.classifier_result or result.judge_result
            self._audit.log_tool_screen(
                tool_name=tool_name,
                text=text,
                blocked=True,
                source=blocker.source if blocker else "unknown",
                confidence=blocker.confidence if blocker else None,
            )

        return result

    def validate_action(self, tool_name: str, tool_args: dict) -> ActionDecision:
        """Validate a proposed tool action against the policy engine (stage 3).

        Returns an ActionDecision with the verdict.
        """
        if not self._policy:
            return ActionDecision(
                verdict=ActionVerdict.ALLOW,
                tool_name=tool_name,
                reason="Policy engine disabled",
            )

        decision = self._policy.evaluate(tool_name, tool_args)

        if decision.verdict == ActionVerdict.REVIEW:
            logger.warning("Action flagged for review: %s — %s", tool_name, decision.reason)
        elif decision.verdict == ActionVerdict.DENY:
            logger.warning("Action denied: %s — %s", tool_name, decision.reason)

        # Audit
        if self._audit:
            self._audit.log_action(
                tool_name=tool_name,
                arg_keys=list(tool_args.keys()),
                verdict=decision.verdict.value,
                matched_rule=decision.matched_rule,
            )

        return decision

    def check_coherence(
        self,
        user_request: str,
        tool_name: str,
        tool_args: dict,
        prior_tools: list[str] | None = None,
        available_tools: list[str] | None = None,
    ) -> CoherenceResult:
        """Check if a tool call is coherent with the user's original request.

        Returns a CoherenceResult. When coherence checking is disabled,
        returns coherent=True.
        """
        result = self._coherence.check(
            user_request,
            tool_name,
            tool_args,
            prior_tools=prior_tools,
            available_tools=available_tools,
        )

        if not result.coherent:
            logger.warning("Action coherence failed: %s — %s", tool_name, result.reasoning)

        if self._audit:
            self._audit.log_coherence_check(
                tool_name=tool_name,
                coherent=result.coherent,
                confidence=result.confidence,
            )

        return result

    def check_drift(
        self,
        tool_name: str,
        output_length: int,
        success: bool,
    ) -> list:
        """Run drift detection checks against behavioral baseline.

        Returns a list of DriftAlert objects for any anomalies detected.
        Logs drift events to the audit log.
        """
        if not self._drift:
            return []

        alerts = self._drift.check_all(tool_name, output_length, success)

        for alert in alerts:
            if self._audit:
                self._audit.log_drift_alert(
                    alert_type=alert.alert_type,
                    tool_name=alert.tool_name,
                    detail=alert.detail,
                    severity=alert.severity,
                )

        return alerts

    def scan_tool_output_credentials(self, tool_name: str, output: str) -> list[CredentialMatch]:
        """Scan tool output for leaked credentials (API keys, tokens, etc.).

        Returns a list of CredentialMatch objects for any detected patterns.
        Logs findings to the audit log.
        """
        matches = scan_for_credentials(output)

        if matches and self._audit:
            self._audit.log_credential_leak(
                tool_name=tool_name,
                patterns_found=[
                    {"pattern": m.pattern_name, "redacted": m.matched_text} for m in matches
                ],
                count=len(matches),
            )

        return matches

    def log_action_outcome(self, tool_name: str, verdict: str, outcome: str) -> None:
        """Log the final outcome of an action after user review.

        Args:
            tool_name: The tool that was evaluated.
            verdict: The original policy verdict (review/deny).
            outcome: What actually happened — "approved", "denied_by_user",
                     or "denied_by_policy".
        """
        if self._audit:
            self._audit.log_action_outcome(
                tool_name=tool_name,
                verdict=verdict,
                outcome=outcome,
            )

    # --- Network monitoring ---

    @property
    def network_monitor(self) -> NetworkMonitor | None:
        """Return the active NetworkMonitor, or None if disabled."""
        return self._network

    def check_network_request(
        self,
        url: str,
        *,
        executor: str = "",
        method: str = "GET",
        request_size_bytes: int = 0,
    ) -> NetworkVerdict:
        """Check an outbound network request against the network policy.

        If the network monitor is disabled, all requests are allowed.
        NetworkMonitor handles in-memory logging; this method handles
        JSONL audit logging only.
        """
        if not self._network:
            return NetworkVerdict(allowed=True)

        verdict = self._network.check_request(
            url,
            executor=executor,
            method=method,
            request_size_bytes=request_size_bytes,
        )

        if self._audit:
            domain = verdict.domain
            self._audit.log_network_request(
                url=url,
                domain=domain,
                executor=executor,
                method=method,
                request_size_bytes=request_size_bytes,
                blocked=not verdict.allowed,
                block_reason=verdict.reason,
            )

            if not verdict.allowed:
                alert_type = "unknown_domain" if verdict.is_unknown_domain else "policy_violation"
                self._audit.log_network_alert(
                    alert_type=alert_type,
                    executor=executor,
                    detail=verdict.reason,
                    url=url,
                    domain=domain,
                )

        if not verdict.allowed:
            logger.warning(
                "Network request denied: %s (executor=%s, reason=%s)",
                url,
                executor,
                verdict.reason,
            )

        return verdict

    def record_network_response(
        self,
        url: str,
        *,
        executor: str = "",
        method: str = "GET",
        request_size_bytes: int = 0,
        response_size_bytes: int = 0,
        status_code: int | None = None,
    ) -> None:
        """Record a completed network response and audit-log it.

        Alerts on oversized responses. NetworkMonitor handles in-memory
        logging; this method handles JSONL audit logging only.
        """
        if not self._network:
            return

        alert = self._network.record_response(
            url,
            executor=executor,
            method=method,
            request_size_bytes=request_size_bytes,
            response_size_bytes=response_size_bytes,
            status_code=status_code,
        )

        domain = _extract_domain(url)

        if self._audit:
            self._audit.log_network_request(
                url=url,
                domain=domain,
                executor=executor,
                method=method,
                request_size_bytes=request_size_bytes,
                response_size_bytes=response_size_bytes,
                status_code=status_code,
                blocked=False,
            )

            if alert and not alert.allowed:
                self._audit.log_network_alert(
                    alert_type="large_response",
                    executor=executor,
                    detail=alert.reason,
                    url=url,
                    domain=domain,
                )

    async def run_pipeline(self, context: PipelineContext) -> PipelineResult:
        """Run the configured parallel/sequential check pipeline.

        Executes checks defined in ``GuardianConfig.pipeline`` — parallel
        checks run concurrently, sequential checks run afterwards.  Supports
        short-circuiting on first block and a pipeline-level timeout.

        Args:
            context: A :class:`PipelineContext` with the input data that
                individual checks will read from.

        Returns:
            A :class:`PipelineResult` indicating whether the input was
            blocked, which checks ran, and timing information.
        """
        return await self._pipeline.run(context)
