"""Fast classifier — local DeBERTa prompt-injection detector."""

from __future__ import annotations

import logging
import time

from guardian.types import ClassifierResult, FastClassifierConfig

logger = logging.getLogger(__name__)

CHUNK_SIZE = 2048


class FastClassifier:
    """DeBERTa-based prompt-injection classifier.

    Tries to load the model in this order:
    1. ``optimum`` ONNX runtime (fastest)
    2. Bare ``transformers`` pipeline (torch)

    Raises ``RuntimeError`` if neither backend is available.
    The model is loaded eagerly at construction time, or via ``warm_up()``.
    """

    def __init__(self, config: FastClassifierConfig) -> None:
        self._config = config
        self._pipeline: object | None = None
        self._backend: str = "none"  # "onnx" | "transformers" | "none"
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
            self._backend = "onnx"
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
            self._backend = "transformers"
            logger.info("Fast classifier loaded via transformers: %s", model_name)
            return
        except Exception:
            logger.debug("transformers not available")

        # Neither available — refuse to run silently without a classifier
        raise RuntimeError(
            "Fast classifier requires transformers or optimum+onnxruntime. "
            "Install the dependencies or run with guardian disabled."
        )

    def warm_up(self) -> None:
        """Run a throwaway inference to avoid cold-start latency.

        Safe to call multiple times (no-op if already loaded or disabled).
        """
        if not self._config.enabled or self._pipeline is None:
            return

        t0 = time.perf_counter()
        try:
            self._pipeline("warmup")
        except Exception:
            pass
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "Fast classifier warm-up complete (backend=%s, %.1fms)",
            self._backend,
            elapsed_ms,
        )

    @property
    def backend(self) -> str:
        """Return which backend is active: ``"onnx"``, ``"transformers"``, or ``"none"``."""
        return self._backend

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
        t0 = time.perf_counter()

        for idx, chunk in enumerate(chunks):
            try:
                results = self._pipeline(chunk)
                top = results[0]
                label = top["label"].upper()
                score = top["score"]

                is_injection = label == "INJECTION" and score >= self._config.threshold
                confidence = score if label == "INJECTION" else 1.0 - score

                logger.debug(
                    "chunk %d/%d: label=%s score=%.4f is_injection=%s",
                    idx + 1, len(chunks), label, score, is_injection,
                )

                elapsed_ms = (time.perf_counter() - t0) * 1000
                result = ClassifierResult(
                    is_injection=is_injection,
                    confidence=confidence,
                    source="fast_classifier",
                    reasoning=f"label={label}, score={score:.4f}, {elapsed_ms:.1f}ms",
                )

                # Short-circuit: injection found
                if is_injection:
                    logger.info(
                        "Fast classifier: INJECTION label=%s score=%.4f elapsed=%.1fms chunks=%d",
                        label, score, elapsed_ms, idx + 1,
                    )
                    return result

                if best is None or confidence > best.confidence:
                    best = result
            except Exception:
                logger.warning("Fast classifier inference failed on chunk", exc_info=True)
                continue

        elapsed_ms = (time.perf_counter() - t0) * 1000
        if best:
            logger.info(
                "Fast classifier: SAFE confidence=%.4f elapsed=%.1fms chunks=%d",
                best.confidence, elapsed_ms, len(chunks),
            )

        return best

    def classify_detailed(self, text: str) -> tuple[ClassifierResult | None, list[dict]]:
        """Classify text and return per-chunk diagnostic details.

        Same logic as ``classify()`` but collects a details list with one
        entry per chunk for debugging false positives.

        Returns ``(result, chunk_details)`` where *chunk_details* is a list
        of dicts like::

            {"index": 0, "length": 137, "label": "INJECTION",
             "score": 0.9953, "is_injection": True}
        """
        if not self._config.enabled:
            return None, []

        chunks = [text[i : i + CHUNK_SIZE] for i in range(0, max(len(text), 1), CHUNK_SIZE)]

        best: ClassifierResult | None = None
        chunk_details: list[dict] = []

        for idx, chunk in enumerate(chunks):
            try:
                results = self._pipeline(chunk)
                top = results[0]
                label = top["label"].upper()
                score = top["score"]

                is_injection = label == "INJECTION" and score >= self._config.threshold
                confidence = score if label == "INJECTION" else 1.0 - score

                chunk_details.append({
                    "index": idx,
                    "length": len(chunk),
                    "label": label,
                    "score": round(score, 4),
                    "is_injection": is_injection,
                })

                logger.debug(
                    "chunk %d/%d: label=%s score=%.4f is_injection=%s",
                    idx + 1, len(chunks), label, score, is_injection,
                )

                result = ClassifierResult(
                    is_injection=is_injection,
                    confidence=confidence,
                    source="fast_classifier",
                    reasoning=f"label={label}, score={score:.4f}",
                )

                if is_injection:
                    return result, chunk_details

                if best is None or confidence > best.confidence:
                    best = result
            except Exception:
                logger.warning("Fast classifier inference failed on chunk", exc_info=True)
                continue

        return best, chunk_details
