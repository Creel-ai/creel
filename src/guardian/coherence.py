"""Action coherence check — verify tool calls match user intent.

Uses a cheap LLM call (Haiku) to compare the user's original request
against the proposed tool call.  Catches cases where prompt injection
causes the agent to call tools unrelated to what the user asked.
"""

from __future__ import annotations

import json
import logging
import time

from guardian.types import CoherenceConfig, CoherenceResult

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a security checker. Your job is to determine whether a tool call \
is coherent with the user's original request.

You will be given:
1. The user's original request
2. The tool being called and its arguments

Determine if the tool call is a reasonable action to fulfill the user's request.

Respond with ONLY a JSON object:
{"coherent": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation"}

Examples of INCOHERENT tool calls:
- User asks "what's the weather?" but agent calls send_email
- User asks to "read my emails" but agent calls delete_file
- User asks a simple question but agent calls upload_file

IMPORTANT: The agent often fulfills multi-part requests with SEQUENTIAL tool calls. \
If the user asks for TWO things (e.g. "check weather AND my calendar"), the agent \
will call one tool at a time. A tool call that addresses ANY part of the request \
is coherent — it does NOT need to address ALL parts in a single call.

Be generous: if the tool call is even loosely related to the request, it's coherent."""


class CoherenceChecker:
    """LLM-based action coherence checker.

    Compares the user's request against a proposed tool call to catch
    cases where injection causes unrelated tool execution.
    """

    def __init__(self, config: CoherenceConfig) -> None:
        self._config = config
        self._total_calls = 0
        self._total_latency_ms = 0.0

    @property
    def usage_stats(self) -> dict:
        return {
            "calls": self._total_calls,
            "total_latency_ms": round(self._total_latency_ms, 1),
        }

    def check(
        self,
        user_request: str,
        tool_name: str,
        tool_args: dict,
    ) -> CoherenceResult:
        """Check if a tool call is coherent with the user's request.

        Returns a CoherenceResult. On any failure, defaults to coherent
        (fail-open) to avoid blocking legitimate actions.
        """
        if not self._config.enabled:
            return CoherenceResult(coherent=True, confidence=1.0, reasoning="Coherence check disabled")

        # Skip coherence check for cleanup/housekeeping tools
        _SKIP_COHERENCE = {"browser_close", "browser_sessions", "mark_read"}
        if tool_name in _SKIP_COHERENCE:
            return CoherenceResult(coherent=True, confidence=1.0, reasoning=f"Skipped: {tool_name} is a cleanup tool")

        t0 = time.perf_counter()
        try:
            from taskrunner.llm import _get_client

            user_msg = (
                f"User request: {user_request}\n\n"
                f"Tool call: {tool_name}({json.dumps(tool_args, default=str)})"
            )

            client = _get_client()
            response = client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                timeout=self._config.timeout,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self._total_calls += 1
            self._total_latency_ms += elapsed_ms

            raw_text = ""
            for block in response.content:
                if block.type == "text":
                    raw_text += block.text

            # Try to extract JSON from response — LLM sometimes wraps it in markdown
            try:
                result = json.loads(raw_text)
            except json.JSONDecodeError:
                import re
                match = re.search(r"\{[^}]+\}", raw_text)
                if match:
                    result = json.loads(match.group())
                else:
                    raise

            coherent = bool(result.get("coherent", True))
            confidence = float(result.get("confidence", 0.5))
            reasoning = str(result.get("reasoning", ""))

            logger.info(
                "Coherence check: coherent=%s confidence=%.3f elapsed=%.1fms tool=%s",
                coherent, confidence, elapsed_ms, tool_name,
            )

            return CoherenceResult(
                coherent=coherent,
                confidence=confidence,
                reasoning=reasoning,
            )

        except Exception:
            logger.warning("Coherence check failed — defaulting to coherent", exc_info=True)
            return CoherenceResult(
                coherent=True,
                confidence=0.0,
                reasoning="Coherence check failed — falling through",
            )
