"""Tests for ImageBuildCache, collect_required_images, and prebuild_images."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from creel.models import AgentDefinition, SkillOverride
from creel.orchestrator import (
    ImageBuildCache,
    _image_cache,
    collect_required_images,
    prebuild_images,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_agent_def(**overrides) -> AgentDefinition:
    """Build a minimal AgentDefinition for testing."""
    defaults = {
        "system_prompt": "test",
        "skills": {},
    }
    defaults.update(overrides)
    return AgentDefinition(**defaults)


def _skill(image: str | None = None, **kwargs) -> SkillOverride:
    return SkillOverride(
        enabled=True,
        image=image,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# collect_required_images
# ---------------------------------------------------------------------------


class TestCollectRequiredImages:
    def test_extracts_images_from_skills(self):
        agent = _minimal_agent_def(
            skills={
                "weather": _skill(),
                "gmail_readonly": _skill(),
            }
        )
        images = collect_required_images(agent)
        assert "executor-weather:latest" in images
        assert "executor-gmail-readonly:latest" in images
        assert "llm-runner:latest" in images

    def test_respects_image_override(self):
        agent = _minimal_agent_def(
            skills={
                "weather": _skill(image="my-custom-image:v2"),
            }
        )
        images = collect_required_images(agent)
        assert "my-custom-image:v2" in images
        assert "executor-weather:latest" not in images

    def test_includes_llm_runner(self):
        agent = _minimal_agent_def(
            skills={
                "weather": _skill(),
            }
        )
        images = collect_required_images(agent)
        assert "llm-runner:latest" in images

    def test_no_skills_no_images(self):
        agent = _minimal_agent_def(skills={})
        images = collect_required_images(agent)
        assert images == []

    def test_sorted_output(self):
        agent = _minimal_agent_def(
            skills={
                "zebra": _skill(),
                "apple": _skill(),
            }
        )
        images = collect_required_images(agent)
        assert images == sorted(images)


# ---------------------------------------------------------------------------
# ImageBuildCache
# ---------------------------------------------------------------------------


class TestImageBuildCache:
    def setup_method(self):
        self.cache = ImageBuildCache()

    @patch("creel.containers._ensure_image_uncached")
    def test_deduplication(self, mock_build):
        """Two concurrent threads trigger only one build."""
        build_started = threading.Event()
        build_proceed = threading.Event()

        def slow_build(image):
            build_started.set()
            build_proceed.wait(timeout=5)
            return f"built-{image}"

        mock_build.side_effect = slow_build

        results = [None, None]
        errors = [None, None]

        def thread_fn(idx):
            try:
                results[idx] = self.cache.ensure_image("executor-weather:latest")
            except Exception as e:
                errors[idx] = e

        t1 = threading.Thread(target=thread_fn, args=(0,))
        t1.start()
        # Wait for the first thread to start building
        build_started.wait(timeout=5)

        # Start second thread while the first is mid-build
        t2 = threading.Thread(target=thread_fn, args=(1,))
        t2.start()

        # Let the build finish
        build_proceed.set()

        t1.join(timeout=10)
        t2.join(timeout=10)

        assert errors == [None, None]
        assert results[0] == results[1]
        assert mock_build.call_count == 1

    @patch("creel.containers._ensure_image_uncached")
    def test_error_propagation(self, mock_build):
        """Build error is stored and re-raised to waiters."""
        build_started = threading.Event()
        build_proceed = threading.Event()

        def failing_build(image):
            build_started.set()
            build_proceed.wait(timeout=5)
            raise RuntimeError("docker daemon down")

        mock_build.side_effect = failing_build

        errors = [None, None]

        def thread_fn(idx):
            try:
                self.cache.ensure_image("executor-weather:latest")
            except Exception as e:
                errors[idx] = e

        t1 = threading.Thread(target=thread_fn, args=(0,))
        t1.start()
        build_started.wait(timeout=5)

        t2 = threading.Thread(target=thread_fn, args=(1,))
        t2.start()

        build_proceed.set()

        t1.join(timeout=10)
        t2.join(timeout=10)

        assert all(isinstance(e, RuntimeError) for e in errors)

    @patch("creel.containers._ensure_image_uncached")
    def test_retry_after_failure(self, mock_build):
        """Failed pre-build doesn't permanently block on-demand calls."""
        mock_build.side_effect = RuntimeError("transient failure")

        with pytest.raises(RuntimeError, match="transient failure"):
            self.cache.ensure_image("executor-weather:latest")

        # Retry should call the build again (entry was cleared)
        mock_build.side_effect = lambda img: f"built-{img}"
        result = self.cache.ensure_image("executor-weather:latest")
        assert result == "built-executor-weather:latest"
        assert mock_build.call_count == 2

    @patch("creel.containers._ensure_image_uncached")
    def test_cache_hit(self, mock_build):
        """Successful build is cached on subsequent calls."""
        mock_build.return_value = "executor-weather:abc123"

        r1 = self.cache.ensure_image("executor-weather:latest")
        r2 = self.cache.ensure_image("executor-weather:latest")

        assert r1 == r2 == "executor-weather:abc123"
        assert mock_build.call_count == 1

    @patch("creel.containers._ensure_image_uncached")
    def test_different_images_independent(self, mock_build):
        """Different image keys are built independently."""
        mock_build.side_effect = lambda img: f"built-{img}"

        r1 = self.cache.ensure_image("executor-weather:latest")
        r2 = self.cache.ensure_image("llm-runner:latest")

        assert r1 == "built-executor-weather:latest"
        assert r2 == "built-llm-runner:latest"
        assert mock_build.call_count == 2

    @patch("creel.containers._ensure_image_uncached")
    def test_waiter_handles_superseded_entry(self, mock_build):
        """Waiter re-enters ensure_image when its entry is superseded.

        Simulates the TOCTOU race: waiter blocks on event_a.wait(), a
        retry thread removes the entry, then event_a fires.  The waiter
        should detect that its entry is gone and re-enter rather than
        reading stale state.
        """
        import time

        mock_build.return_value = "built-executor-weather:latest"

        # Place an in-progress entry; the waiter will block on this event
        event_a = threading.Event()
        self.cache._builds["executor-weather"] = (event_a, None, None)

        results = [None]
        errors = [None]

        def waiter():
            try:
                results[0] = self.cache.ensure_image("executor-weather:latest")
            except Exception as e:
                errors[0] = e

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)  # Let waiter reach event_a.wait()

        # Simulate retry thread removing the failed entry while waiter is blocked
        with self.cache._lock:
            del self.cache._builds["executor-weather"]

        event_a.set()  # Wake the waiter
        t.join(timeout=10)

        # Waiter should have re-entered and built successfully
        assert errors[0] is None
        assert results[0] == "built-executor-weather:latest"

    def test_clear(self):
        """clear() removes all entries."""
        self.cache._builds["test"] = (threading.Event(), "result", None)
        self.cache.clear()
        assert len(self.cache._builds) == 0


# ---------------------------------------------------------------------------
# prebuild_images
# ---------------------------------------------------------------------------


class TestPrebuildImages:
    def setup_method(self):
        _image_cache.clear()

    def teardown_method(self):
        _image_cache.clear()

    @patch("creel.containers._ensure_image_uncached")
    def test_starts_background_threads(self, mock_build):
        """prebuild_images() spawns and completes daemon threads."""
        mock_build.side_effect = lambda img: f"built-{img}"

        agent = _minimal_agent_def(
            skills={
                "weather": _skill(),
                "gmail_readonly": _skill(),
            }
        )

        threads = prebuild_images(agent)
        assert len(threads) > 0

        for t in threads:
            assert t.daemon
            t.join(timeout=10)

        # All images were built
        built_images = {c.args[0] for c in mock_build.call_args_list}
        assert "executor-weather:latest" in built_images
        assert "executor-gmail-readonly:latest" in built_images
        assert "llm-runner:latest" in built_images
