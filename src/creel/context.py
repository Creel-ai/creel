"""Context pruning — intelligent context window management.

Provides token estimation, importance-based message scoring, and
automatic pruning with summary compression to keep conversations
within model token limits.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Rough token estimate: ~3 characters per token.
# Conservative (overestimates) to account for JSON-heavy tool results.
_CHARS_PER_TOKEN = 3

# Default pruning threshold — start pruning at 80 % of model max.
DEFAULT_PRUNING_THRESHOLD = 0.80

# Prune down to this fraction of max_tokens to create headroom and avoid
# re-pruning every turn.
_PRUNE_TARGET_FRACTION = 0.60

# Importance weights by message role / content type.
_WEIGHT_USER_TEXT = 1.5
_WEIGHT_ASSISTANT_TEXT = 1.0
_WEIGHT_TOOL_RESULT = 2.0
_WEIGHT_TOOL_USE = 1.8
_WEIGHT_SUMMARY = float("inf")  # never prune summaries

# Exponential decay half-life (in number of messages from the end).
_DECAY_HALF_LIFE = 8

# Default number of recent messages to always protect from pruning.
_DEFAULT_MIN_RECENT = 4


@dataclass
class MessageScore:
    """Score for a single message in the conversation."""

    index: int
    tokens: int
    importance: float
    role: str
    is_prunable: bool


def estimate_tokens(text: str) -> int:
    """Estimate token count from a string using a character heuristic."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def estimate_message_tokens(message: dict) -> int:
    """Estimate the token count for a single Anthropic-format message."""
    content = message.get("content", "")
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += estimate_tokens(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    total += estimate_tokens(block.get("name", ""))
                    total += estimate_tokens(json.dumps(block.get("input", {})))
                elif block.get("type") == "tool_result":
                    total += estimate_tokens(str(block.get("content", "")))
        return max(1, total)
    return 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens for a list of messages."""
    return sum(estimate_message_tokens(m) for m in messages)


def _is_summary_message(message: dict) -> bool:
    """Check if a message is a conversation summary block."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.startswith("[CONVERSATION SUMMARY]")
    return False


def _message_weight(message: dict) -> float:
    """Compute the base importance weight for a message based on its type."""
    if _is_summary_message(message):
        return _WEIGHT_SUMMARY

    role = message.get("role", "")
    content = message.get("content", "")

    if role == "user":
        if isinstance(content, list):
            # Check if it's a tool_result message
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if has_tool_result:
                return _WEIGHT_TOOL_RESULT
        return _WEIGHT_USER_TEXT

    if role == "assistant":
        if isinstance(content, list):
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
            if has_tool_use:
                return _WEIGHT_TOOL_USE
        return _WEIGHT_ASSISTANT_TEXT

    return 1.0


def score_messages(messages: list[dict]) -> list[MessageScore]:
    """Score each message by importance (higher = more important to keep).

    Scoring combines:
    - Base weight by message type (tool results > user text > assistant text)
    - Exponential recency decay (recent messages score higher)
    """
    n = len(messages)
    scores: list[MessageScore] = []

    for i, msg in enumerate(messages):
        tokens = estimate_message_tokens(msg)
        base_weight = _message_weight(msg)
        is_prunable = not math.isinf(base_weight)

        # Recency: messages closer to the end get higher multiplier.
        # distance_from_end=0 -> multiplier=1.0, distance=half_life -> 0.5
        distance_from_end = n - 1 - i
        recency = math.pow(0.5, distance_from_end / _DECAY_HALF_LIFE)

        importance = base_weight * recency if is_prunable else float("inf")

        scores.append(
            MessageScore(
                index=i,
                tokens=tokens,
                importance=importance,
                role=msg.get("role", ""),
                is_prunable=is_prunable,
            )
        )

    return scores


def _is_tool_pair_boundary(messages: list[dict], idx: int) -> bool:
    """Check whether index ``idx`` is the first message of a tool-call pair.

    A tool-call pair is:
      messages[idx]   = assistant with tool_use block(s)
      messages[idx+1] = user with tool_result block(s)

    We must never prune only one half of a pair.
    """
    if idx + 1 >= len(messages):
        return False
    msg = messages[idx]
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content", [])
    if not isinstance(content, list):
        return False
    has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
    if not has_tool_use:
        return False
    next_msg = messages[idx + 1]
    if next_msg.get("role") != "user":
        return False
    next_content = next_msg.get("content", [])
    if not isinstance(next_content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in next_content)


def select_messages_to_prune(
    messages: list[dict],
    target_tokens: int,
    current_tokens: int | None = None,
    min_recent: int = _DEFAULT_MIN_RECENT,
) -> list[int]:
    """Select message indices to prune to get under ``target_tokens``.

    Preserves:
    - The first message (system/summary context)
    - The most recent ``min_recent`` messages
    - Tool-call pairs (never splits assistant tool_use from user tool_result)

    Returns indices sorted ascending.
    """
    if current_tokens is None:
        current_tokens = estimate_messages_tokens(messages)

    if current_tokens <= target_tokens:
        return []

    scores = score_messages(messages)
    n = len(messages)

    # Never prune first message or last min_recent messages.
    protected = {0} | {i for i in range(max(1, n - min_recent), n)}

    # Build tool-pair groups: indices that must be pruned together.
    pair_groups: dict[int, list[int]] = {}  # leader_idx -> [idx1, idx2]
    # Reverse index: idx -> leader_idx for O(1) lookup.
    idx_to_leader: dict[int, int] = {}
    i = 0
    while i < n:
        if _is_tool_pair_boundary(messages, i):
            pair_groups[i] = [i, i + 1]
            idx_to_leader[i] = i
            idx_to_leader[i + 1] = i
            i += 2
        else:
            i += 1

    # Candidates: prunable scores sorted by importance ascending (prune least important first).
    candidates = []
    visited: set[int] = set()
    for s in scores:
        if s.index in protected or s.index in visited or not s.is_prunable:
            continue
        # Check if this index is part of a pair group via reverse index.
        group_leader = idx_to_leader.get(s.index)
        if group_leader is not None:
            # Add the whole group.
            group = pair_groups[group_leader]
            if any(idx in protected for idx in group):
                continue
            group_tokens = sum(scores[idx].tokens for idx in group)
            group_importance = min(scores[idx].importance for idx in group)
            candidates.append((group_importance, group_tokens, group))
            visited.update(group)
        else:
            candidates.append((s.importance, s.tokens, [s.index]))
            visited.add(s.index)

    candidates.sort(key=lambda c: c[0])

    to_prune: list[int] = []
    tokens_freed = 0
    tokens_needed = current_tokens - target_tokens

    for _importance, group_tokens, group_indices in candidates:
        if tokens_freed >= tokens_needed:
            break
        to_prune.extend(group_indices)
        tokens_freed += group_tokens

    to_prune.sort()
    return to_prune


def _sanitize_summary_xml(text: str) -> str:
    """Escape </summary> in summary text to prevent breaking the XML wrapper."""
    return text.replace("</summary>", "&lt;/summary&gt;")


def prune_messages(
    messages: list[dict],
    max_tokens: int,
    threshold: float = DEFAULT_PRUNING_THRESHOLD,
    summarize_fn=None,
    min_recent: int = _DEFAULT_MIN_RECENT,
) -> tuple[list[dict], str | None]:
    """Prune messages to stay within the token budget.

    Args:
        messages: Conversation messages (Anthropic format).
        max_tokens: Model's maximum context tokens.
        threshold: Fraction of max_tokens at which pruning is triggered.
            Once triggered, messages are pruned down to ``_PRUNE_TARGET_FRACTION``
            of max_tokens to create headroom.
        summarize_fn: Optional callable(list[dict]) -> str for summarizing
            pruned messages. If provided, pruned messages are summarized
            and the summary is prepended.
        min_recent: Minimum number of recent messages to always keep.

    Returns:
        (pruned_messages, summary_text_or_None)
    """
    trigger = int(max_tokens * threshold)
    current = estimate_messages_tokens(messages)

    if current <= trigger:
        return messages, None

    # Prune to a lower target to create headroom.
    target = int(max_tokens * _PRUNE_TARGET_FRACTION)

    indices_to_prune = select_messages_to_prune(messages, target, current, min_recent=min_recent)
    if not indices_to_prune:
        return messages, None

    pruned_msgs = [messages[i] for i in sorted(indices_to_prune)]
    kept = [msg for i, msg in enumerate(messages) if i not in set(indices_to_prune)]

    summary_text = None
    if summarize_fn and pruned_msgs:
        try:
            raw_summary = summarize_fn(pruned_msgs)
            safe_summary = _sanitize_summary_xml(raw_summary)
            summary_msg = {
                "role": "user",
                "content": f"[CONVERSATION SUMMARY]\n<summary>\n{safe_summary}\n</summary>",
            }
            # Insert summary at the beginning (after any existing summary).
            if kept and _is_summary_message(kept[0]):
                # Merge with existing summary.
                existing = kept[0].get("content", "")
                if isinstance(existing, str):
                    merged = existing.replace("</summary>", f"\n{safe_summary}\n</summary>")
                    kept[0] = {"role": "user", "content": merged}
                    summary_text = merged
                else:
                    kept.insert(0, summary_msg)
            else:
                kept.insert(0, summary_msg)
            if summary_text is None:
                summary_text = safe_summary
        except Exception:
            logger.warning("Summary compression failed during pruning", exc_info=True)

    logger.info(
        "Pruned %d messages (%d tokens freed), %d remaining",
        len(indices_to_prune),
        current - estimate_messages_tokens(kept),
        len(kept),
    )
    return kept, summary_text
