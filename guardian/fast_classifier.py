"""Fast classifier — local DeBERTa prompt-injection detector."""

from __future__ import annotations

import logging

from guardian.types import ClassifierResult, FastClassifierConfig

logger = logging.getLogger(__name__)

CHUNK_SIZE = 2048


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

        Splits the input into ``CHUNK_SIZE``-character chunks and classifies
        each one.  Short-circuits immediately when any chunk crosses the
        injection threshold.  For all-safe inputs the chunk with the highest
        injection confidence is returned.

        Returns ``None`` if the classifier is unavailable or disabled.
        """
        if not self._config.enabled:
            return None

        chunks = [text[i : i + CHUNK_SIZE] for i in range(0, max(len(text), 1), CHUNK_SIZE)]

        best: ClassifierResult | None = None

        for chunk in chunks:
            try:
                results = self._pipeline(chunk)
                # Pipeline returns [{"label": "INJECTION"/"SAFE", "score": 0.99}]
                top = results[0]
                label = top["label"].upper()
                score = top["score"]

                is_injection = label == "INJECTION" and score >= self._config.threshold
                confidence = score if label == "INJECTION" else 1.0 - score

                result = ClassifierResult(
                    is_injection=is_injection,
                    confidence=confidence,
                    source="fast_classifier",
                    reasoning=f"label={label}, score={score:.4f}",
                )

                # Short-circuit: injection found
                if is_injection:
                    return result

                # Track highest injection confidence across safe chunks
                if best is None or confidence > best.confidence:
                    best = result
            except Exception:
                logger.warning("Fast classifier inference failed on chunk", exc_info=True)
                continue

        return best
