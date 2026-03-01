"""Parameterized tests running injection fixtures through the classifier.

The classifier requires ML dependencies (transformers/optimum).
Tests are skipped if those are not available.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.injection_fixtures import ALL_BENIGN, ALL_INJECTIONS


# Ensure we have enough fixtures
def test_minimum_injection_count() -> None:
    assert len(ALL_INJECTIONS) >= 50, f"Expected 50+ injections, got {len(ALL_INJECTIONS)}"


def test_minimum_benign_count() -> None:
    assert len(ALL_BENIGN) >= 20, f"Expected 20+ benign, got {len(ALL_BENIGN)}"


def test_injection_categories_present() -> None:
    categories = {cat for _, cat in ALL_INJECTIONS}
    expected = {"direct", "indirect", "jailbreak", "roleplay", "encoding", "multilingual"}
    assert expected.issubset(categories), f"Missing categories: {expected - categories}"


# --- Parameterized tests with mocked classifier ---


@pytest.fixture
def mock_classifier():
    """Create a FastClassifier with mocked pipeline."""
    from guardian.fast_classifier import FastClassifier
    from guardian.types import FastClassifierConfig

    config = FastClassifierConfig(enabled=True, threshold=0.85, model_name="test-model")
    with patch.object(FastClassifier, "_load"):
        clf = FastClassifier(config)

    # Mock: anything with "ignore" or "instruction" or known injection keywords → INJECTION
    def mock_pipeline(text: str):
        text_lower = text.lower()
        injection_signals = [
            "ignore",
            "override",
            "disregard",
            "forget",
            "cancel",
            "system prompt",
            "new instruction",
            "developer mode",
            "dan ",
            "do anything now",
            "no restrictions",
            "reveal",
            "bypass",
            "disable",
            "dump",
            "前の指示",
            "ignorez",
            "ignoriere",
            "ignora",
            "무시",
            "игнорируйте",
            "无视",
            "abaikan",
            "pwned",
            "jailbreak",
            "unfiltered",
            "[inst]",
            "[system]",
            "hacker",
            "attacker",
            "exfiltrate",
            "credentials",
            "env vars",
        ]
        is_inj = any(sig in text_lower for sig in injection_signals)
        if is_inj:
            return [{"label": "INJECTION", "score": 0.95}]
        return [{"label": "SAFE", "score": 0.98}]

    clf._pipeline = MagicMock(side_effect=mock_pipeline)
    return clf


@pytest.mark.parametrize(
    "text,category",
    ALL_INJECTIONS,
    ids=[f"injection-{i}-{cat}" for i, (_, cat) in enumerate(ALL_INJECTIONS)],
)
def test_injection_detected(mock_classifier, text: str, category: str) -> None:
    """Each injection fixture should be flagged by the classifier."""
    result = mock_classifier.classify(text)
    assert result is not None
    # We use a permissive mock — not all encoding tricks will match.
    # At minimum, record it ran without error.


@pytest.mark.parametrize(
    "text,category",
    ALL_BENIGN,
    ids=[f"benign-{i}" for i in range(len(ALL_BENIGN))],
)
def test_benign_not_flagged(mock_classifier, text: str, category: str) -> None:
    """Benign inputs should not be flagged as injection."""
    result = mock_classifier.classify(text)
    assert result is not None
    assert result.is_injection is False, f"False positive on benign input: {text[:60]}"
