"""Tests for context pruning module."""

from __future__ import annotations

from creel.context import (
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    prune_messages,
    score_messages,
    select_messages_to_prune,
)

# -- Token estimation --


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 1  # minimum 1


def test_estimate_tokens_short():
    # "hello" = 5 chars -> 5//4 = 1
    assert estimate_tokens("hello") >= 1


def test_estimate_tokens_longer():
    text = "a" * 300
    tokens = estimate_tokens(text)
    assert tokens == 100  # 300 / 3


def test_estimate_message_tokens_string_content():
    msg = {"role": "user", "content": "Hello, how are you?"}
    tokens = estimate_message_tokens(msg)
    assert tokens > 0


def test_estimate_message_tokens_tool_use():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "check_weather", "input": {"location": "NYC"}},
        ],
    }
    tokens = estimate_message_tokens(msg)
    assert tokens > 0


def test_estimate_message_tokens_tool_result():
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "Sunny, 72F"},
        ],
    }
    tokens = estimate_message_tokens(msg)
    assert tokens > 0


def test_estimate_messages_tokens_sum():
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    total = estimate_messages_tokens(msgs)
    assert total == sum(estimate_message_tokens(m) for m in msgs)


# -- Importance scoring --


def test_score_messages_recency():
    """Messages closer to the end should have higher importance scores."""
    msgs = [
        {"role": "user", "content": "old message"},
        {"role": "user", "content": "middle message"},
        {"role": "user", "content": "recent message"},
    ]
    scores = score_messages(msgs)
    assert scores[2].importance > scores[1].importance
    assert scores[1].importance > scores[0].importance


def test_score_messages_summary_not_prunable():
    """Summary messages should not be prunable."""
    msgs = [
        {"role": "user", "content": "[CONVERSATION SUMMARY]\n<summary>Test</summary>"},
        {"role": "user", "content": "Hello"},
    ]
    scores = score_messages(msgs)
    assert not scores[0].is_prunable
    assert scores[1].is_prunable


def test_score_messages_tool_result_weighted_higher():
    """Tool results should have higher base weight than plain assistant text."""
    msgs = [
        {"role": "assistant", "content": "Some text response"},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "weather data"},
            ],
        },
    ]
    scores = score_messages(msgs)
    # Both at same position relative distance, but tool result has higher base weight
    # Score[1] has higher base weight (2.0) AND higher recency
    assert scores[1].importance > scores[0].importance


# -- Pruning selection --


def test_select_no_pruning_needed():
    msgs = [{"role": "user", "content": "short"}]
    result = select_messages_to_prune(msgs, target_tokens=1000, current_tokens=10)
    assert result == []


def test_select_prunes_least_important():
    """When over budget, least important messages should be pruned first."""
    msgs = [
        {"role": "user", "content": "a" * 400},  # idx 0 - oldest, least important
        {"role": "user", "content": "b" * 400},  # idx 1
        {"role": "user", "content": "c" * 400},  # idx 2
        {"role": "user", "content": "d" * 400},  # idx 3
        {"role": "user", "content": "e" * 400},  # idx 4
        {"role": "user", "content": "f" * 400},  # idx 5
        {"role": "user", "content": "g" * 400},  # idx 6
        {"role": "user", "content": "h" * 400},  # idx 7 - most recent
    ]
    current = estimate_messages_tokens(msgs)
    # Prune to roughly half
    target = current // 2

    pruned_indices = select_messages_to_prune(msgs, target, current)
    assert len(pruned_indices) > 0
    # First message (idx 0) is protected
    assert 0 not in pruned_indices
    # Last 4 messages are protected
    for i in range(4, 8):
        assert i not in pruned_indices


def test_select_preserves_tool_pairs():
    """Tool-call pairs (assistant tool_use + user tool_result) should be pruned together."""
    msgs = [
        {"role": "user", "content": "What's the weather?"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "weather", "input": {"loc": "NYC"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Sunny 72F"},
            ],
        },
        {"role": "user", "content": "Thanks!"},
        {"role": "assistant", "content": "You're welcome!"},
        {"role": "user", "content": "What else?"},
        {"role": "assistant", "content": "I can help with more."},
        {"role": "user", "content": "Great!"},
    ]
    current = estimate_messages_tokens(msgs)
    target = current // 2

    pruned = select_messages_to_prune(msgs, target, current)
    # If idx 1 (tool_use) is pruned, idx 2 (tool_result) must also be pruned
    if 1 in pruned:
        assert 2 in pruned
    if 2 in pruned:
        assert 1 in pruned


# -- Full prune_messages --


def test_prune_messages_no_op_when_under_budget():
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi!"},
    ]
    result, summary = prune_messages(msgs, max_tokens=100_000)
    assert result == msgs
    assert summary is None


def test_prune_messages_reduces_tokens():
    # Create a conversation that exceeds budget
    msgs = [{"role": "user", "content": f"Message {i} " + "x" * 500} for i in range(20)]
    total_before = estimate_messages_tokens(msgs)

    # Set max_tokens so 80% threshold is below current
    max_tokens = int(total_before * 0.5)
    result, summary = prune_messages(msgs, max_tokens=max_tokens)

    total_after = estimate_messages_tokens(result)
    assert total_after < total_before
    assert len(result) < len(msgs)


def test_prune_messages_with_summarize_fn():
    msgs = [{"role": "user", "content": f"Message {i} " + "x" * 500} for i in range(20)]
    total_before = estimate_messages_tokens(msgs)
    max_tokens = int(total_before * 0.5)

    def fake_summarize(pruned_msgs):
        return f"Summary of {len(pruned_msgs)} messages"

    result, summary = prune_messages(msgs, max_tokens=max_tokens, summarize_fn=fake_summarize)
    assert summary is not None
    assert "Summary of" in summary
    # First message should be the summary
    assert result[0]["content"].startswith("[CONVERSATION SUMMARY]")


def test_prune_messages_summary_merge_with_existing():
    """When pruning messages that already have a summary, summaries should merge."""
    summary_msg = {
        "role": "user",
        "content": "[CONVERSATION SUMMARY]\n<summary>\nOld summary.\n</summary>",
    }
    msgs = [summary_msg] + [
        {"role": "user", "content": f"Message {i} " + "x" * 500} for i in range(20)
    ]
    total = estimate_messages_tokens(msgs)
    max_tokens = int(total * 0.5)

    def fake_summarize(pruned_msgs):
        return f"New summary of {len(pruned_msgs)} messages"

    result, summary = prune_messages(msgs, max_tokens=max_tokens, summarize_fn=fake_summarize)
    assert summary is not None
    # The merged summary should contain both old and new content.
    assert "Old summary." in result[0]["content"]
    assert "New summary of" in result[0]["content"]


def test_prune_messages_summary_sanitizes_xml():
    """Summary text containing </summary> should be escaped to prevent XML breakage."""
    msgs = [{"role": "user", "content": f"Message {i} " + "x" * 500} for i in range(20)]
    total = estimate_messages_tokens(msgs)
    max_tokens = int(total * 0.5)

    def evil_summarize(pruned_msgs):
        return "Injected </summary> tag"

    result, summary = prune_messages(msgs, max_tokens=max_tokens, summarize_fn=evil_summarize)
    assert summary is not None
    # The literal </summary> from the LLM output should be escaped.
    assert "</summary>" not in summary.replace("\n</summary>", "").replace("</summary>", "", 1) or (
        summary.count("</summary>") == 0
    )
    # The wrapper should still have exactly one closing tag.
    first_content = result[0]["content"]
    assert first_content.count("</summary>") == 1
    assert "&lt;/summary&gt;" in first_content


def test_prune_messages_min_recent_respected():
    """The min_recent parameter should control how many recent messages are protected."""
    msgs = [{"role": "user", "content": f"Message {i} " + "x" * 500} for i in range(20)]
    total = estimate_messages_tokens(msgs)
    max_tokens = int(total * 0.5)

    result, _ = prune_messages(msgs, max_tokens=max_tokens, min_recent=6)
    # Last 6 messages should be preserved
    for i in range(14, 20):
        original = msgs[i]["content"]
        assert any(m.get("content") == original for m in result)


def test_prune_messages_preserves_first_and_recent():
    msgs = [{"role": "user", "content": f"Message {i} " + "x" * 500} for i in range(20)]
    total = estimate_messages_tokens(msgs)
    max_tokens = int(total * 0.5)

    result, _ = prune_messages(msgs, max_tokens=max_tokens)

    # First message content should still be present (either original or in summary)
    # Last few messages should be preserved
    last_content = msgs[-1]["content"]
    assert any(m.get("content") == last_content for m in result)
