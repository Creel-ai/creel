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
    3. Unavailable — logs a warning and always returns safe

    The model is loaded lazily on the first ``classify()`` call.
    """

    def __init__(self, config: FastClassifierConfig) -> None:
        self._config = config
        self._pipeline: object | None = None
        self._available: bool | None = None  # None = not yet checked

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
            self._available = True
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
            self._available = True
            logger.info("Fast classifier loaded via transformers: %s", model_name)
            return
        except Exception:
            logger.debug("transformers not available")

        # Neither available
        self._available = False
        logger.warning(
            "Fast classifier unavailable (install transformers or "
            "optimum+onnxruntime). Stage 1 will be skipped."
        )

    def classify(self, text: str) -> ClassifierResult | None:
        """Classify text for prompt injection.

        Returns ``None`` if the classifier is unavailable or disabled.
        """
        if not self._config.enabled:
            return None

        if self._available is None:
            self._load()

        if not self._available:
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
