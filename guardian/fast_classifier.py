"""Fast classifier — local DeBERTa prompt-injection detector."""

from __future__ import annotations

import logging

from guardian.types import ClassifierResult, FastClassifierConfig

logger = logging.getLogger(__name__)


class FastClassifier:
    """DeBERTa-based prompt-injection classifier.

    Tries to load the model in this order:
    1. ``optimum`` ONNX runtime (fastest)
    2. Bare ``transformers`` pipeline (torch)

    Raises ``RuntimeError`` if neither backend is available.
    The model is loaded eagerly at construction time.
    """

    def __init__(self, config: FastClassifierConfig) -> None:
        self._config = config
        self._pipeline: object | None = None
        if self._config.enabled:
            self._load()

    def _load(self) -> None:
        """Attempt to load the classification pipeline."""
        model_name = self._config.model_name

        # Try optimum ONNX first
        try:
            from optimum.onnxruntime import ORTModelForSequenceClassification
            from transformers import AutoTokenizer, pipeline

            tokenizer = AutoTokenizer.from_pretrained(model_name)
            model = ORTModelForSequenceClassification.from_pretrained(model_name)
            self._pipeline = pipeline(
                "text-classification", model=model, tokenizer=tokenizer
            )
            logger.info("Fast classifier loaded via optimum/ONNX: %s", model_name)
            return
        except Exception:
            logger.debug("optimum/ONNX not available, trying bare transformers")

        # Try bare transformers
        try:
            from transformers import pipeline

            self._pipeline = pipeline(
                "text-classification", model=model_name, truncation=True
            )
            logger.info("Fast classifier loaded via transformers: %s", model_name)
            return
        except Exception:
            logger.debug("transformers not available")

        # Neither available — refuse to run silently without a classifier
        raise RuntimeError(
            "Fast classifier requires transformers or optimum+onnxruntime. "
            "Install the dependencies or run with guardian disabled."
        )

    def classify(self, text: str) -> ClassifierResult | None:
        """Classify text for prompt injection.

        Returns ``None`` if the classifier is unavailable or disabled.
        """
        if not self._config.enabled:
            return None

        # Truncate to 512 tokens (rough char estimate for DeBERTa)
        truncated = text[:2048]

        try:
            results = self._pipeline(truncated)
            # Pipeline returns [{"label": "INJECTION"/"SAFE", "score": 0.99}]
            top = results[0]
            label = top["label"].upper()
            score = top["score"]

            is_injection = label == "INJECTION" and score >= self._config.threshold

            return ClassifierResult(
                is_injection=is_injection,
                confidence=score if label == "INJECTION" else 1.0 - score,
                source="fast_classifier",
                reasoning=f"label={label}, score={score:.4f}",
            )
        except Exception:
            logger.warning("Fast classifier inference failed", exc_info=True)
            return None
