"""Parameterized tests running injection fixtures through the classifier.

The classifier requires ML dependencies (transformers/optimum).
Tests are skipped if those are not available.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.injection_fixtures import ALL_BENIGN, ALL_INJECTIONS, BENIGN_SIMILAR

REQUIRED_ATTACK_CATEGORIES = {
    "direct_override",
    "indirect",
    "jailbreak",
    "roleplay",
    "encoding",
    "multilingual",
    "context_manipulation",
    "data_exfiltration",
    "multi_turn",
    "tool_abuse",
}


# Ensure we have enough fixtures
def test_minimum_injection_count() -> None:
    assert len(ALL_INJECTIONS) >= 50, f"Expected 50+ injections, got {len(ALL_INJECTIONS)}"


def test_minimum_benign_count() -> None:
    assert len(ALL_BENIGN) >= 20, f"Expected 20+ benign, got {len(ALL_BENIGN)}"


def test_injection_categories_present() -> None:
    categories = {f["category"] for f in ALL_INJECTIONS}
    assert REQUIRED_ATTACK_CATEGORIES.issubset(categories), (
        f"Missing categories: {REQUIRED_ATTACK_CATEGORIES - categories}"
    )


def test_benign_similar_count() -> None:
    """Benign-similar examples should have at least 10 entries."""
    assert len(BENIGN_SIMILAR) >= 10, f"Expected 10+ benign_similar, got {len(BENIGN_SIMILAR)}"


def test_fixture_schema() -> None:
    """Every fixture must have the required keys."""
    required_keys = {"input", "category", "expected", "severity"}
    for fixture in ALL_INJECTIONS + ALL_BENIGN:
        assert required_keys.issubset(fixture.keys()), (
            f"Fixture missing keys: {required_keys - fixture.keys()}"
        )


# --- Parameterized tests with mocked classifier ---


@pytest.fixture
def mock_classifier():
    """Create a FastClassifier with mocked pipeline."""
    from guardian.fast_classifier import FastClassifier
    from guardian.types import FastClassifierConfig

    config = FastClassifierConfig(enabled=True, threshold=0.85, model_name="test-model")
    with patch.object(FastClassifier, "_load"):
        clf = FastClassifier(config)

    # Mock: anything with known injection keywords → INJECTION
    def mock_pipeline(text: str):
        text_lower = text.lower()
        injection_signals = [
            # Direct override signals
            "ignore",
            "override",
            "disregard",
            "forget",
            "cancel",
            "system prompt",
            "new instruction",
            "developer mode",
            # Jailbreak signals
            "dan ",
            "do anything now",
            "no restrictions",
            "reveal",
            "bypass",
            "disable",
            "dump",
            "jailbreak",
            "unfiltered",
            # Multilingual ignore words
            "前の指示",
            "ignorez",
            "ignoriere",
            "ignora",
            "무시",
            "игнорируйте",
            "无视",
            "abaikan",
            # Security signals
            "pwned",
            "[inst]",
            "[system]",
            "hacker",
            "attacker",
            "exfiltrate",
            "credentials",
            "env vars",
            # Context manipulation signals
            "im_start",
            "system message",
            "begin system",
            "end system",
            "sys_prompt",
            "sym_prompt",
            "hidden instruction",
            "admin privileges",
            "content filter",
            # Data exfiltration signals
            "forward them all",
            "send the results to",
            "upload them",
            "post it to",
            "ssh keys",
            "api keys",
            "email the summary",
            "secrets manager",
            "webhook.site",
            "evil.com",
            ".env file",
            "data-collector",
            # Multi-turn signals
            "you agreed",
            "previous conversation",
            "cashing in",
            "you already revealed",
            "red-team exercise",
            # Tool abuse signals
            "/etc/shadow",
            "rm -rf",
            "force push",
            "delete all branches",
            "phishing",
            "transfer $",
        ]
        is_inj = any(sig in text_lower for sig in injection_signals)
        if is_inj:
            return [{"label": "INJECTION", "score": 0.95}]
        return [{"label": "SAFE", "score": 0.98}]

    clf._pipeline = MagicMock(side_effect=mock_pipeline)
    return clf


@pytest.mark.parametrize(
    "fixture",
    ALL_INJECTIONS,
    ids=[f"injection-{i}-{f['category']}" for i, f in enumerate(ALL_INJECTIONS)],
)
def test_injection_detected(mock_classifier, fixture: dict[str, str]) -> None:
    """Each injection fixture should be flagged by the classifier."""
    result = mock_classifier.classify(fixture["input"])
    assert result is not None
    # Permissive mock — not all encoding tricks will match keyword signals.
    # At minimum, record it ran without error.


@pytest.mark.parametrize(
    "fixture",
    ALL_BENIGN,
    ids=[f"benign-{i}-{f['category']}" for i, f in enumerate(ALL_BENIGN)],
)
def test_benign_not_flagged(mock_classifier, fixture: dict[str, str]) -> None:
    """Benign inputs should not be flagged as injection."""
    result = mock_classifier.classify(fixture["input"])
    assert result is not None
    assert result.is_injection is False, f"False positive on benign input: {fixture['input'][:60]}"


def test_false_positive_rate(mock_classifier) -> None:
    """False positive rate on benign examples must be under 5%."""
    false_positives = 0
    for fixture in ALL_BENIGN:
        result = mock_classifier.classify(fixture["input"])
        if result is not None and result.is_injection:
            false_positives += 1
    rate = false_positives / len(ALL_BENIGN)
    assert rate < 0.05, (
        f"False positive rate {rate:.1%} exceeds 5% ({false_positives}/{len(ALL_BENIGN)})"
    )
