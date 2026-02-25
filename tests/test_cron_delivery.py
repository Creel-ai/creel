"""Tests for cron delivery routing — announce, webhook, and none modes."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from taskrunner.cron.delivery import deliver
from taskrunner.cron.models import CronJob, Delivery, Payload, Schedule


# -- Helpers --


def _make_job(
    name: str = "test job",
    delivery_mode: str = "none",
    channel: str | None = None,
    url: str | None = None,
    best_effort: bool = True,
) -> CronJob:
    delivery_kwargs: dict = {"mode": delivery_mode, "best_effort": best_effort}
    if channel:
        delivery_kwargs["channel"] = channel
    if url:
        delivery_kwargs["url"] = url

    return CronJob(
        name=name,
        schedule=Schedule(kind="cron", expr="0 8 * * *"),
        payload=Payload(message="do stuff"),
        delivery=Delivery(**delivery_kwargs),
    )


# -- None delivery --


class TestDeliverNone:
    def test_none_mode_does_nothing(self):
        """Delivery mode 'none' should return without side effects."""
        job = _make_job(delivery_mode="none")
        # Should not raise
        deliver(delivery=job.delivery, output="hello", job=job)

    def test_none_mode_ignores_channel_send(self):
        """Even with a channel_send callback, none mode should skip delivery."""
        job = _make_job(delivery_mode="none")
        channel_send = MagicMock()
        deliver(
            delivery=job.delivery,
            output="hello",
            job=job,
            channel_send=channel_send,
        )
        channel_send.assert_not_called()


# -- Announce delivery --


class TestDeliverAnnounce:
    def test_announce_calls_channel_send(self):
        """Announce mode should call channel_send with the channel and output."""
        job = _make_job(delivery_mode="announce", channel="whatsapp")
        channel_send = MagicMock()

        deliver(
            delivery=job.delivery,
            output="Job result here",
            job=job,
            channel_send=channel_send,
        )

        channel_send.assert_called_once_with("whatsapp", "Job result here")

    def test_announce_without_callback_raises(self):
        """Announce mode without channel_send should raise RuntimeError."""
        job = _make_job(
            delivery_mode="announce",
            channel="whatsapp",
            best_effort=False,
        )

        with pytest.raises(RuntimeError, match="no channel_send callback"):
            deliver(
                delivery=job.delivery,
                output="hello",
                job=job,
                channel_send=None,
            )

    def test_announce_failure_best_effort_swallowed(self):
        """If channel_send raises and best_effort is True, no exception propagates."""
        job = _make_job(
            delivery_mode="announce",
            channel="whatsapp",
            best_effort=True,
        )
        channel_send = MagicMock(side_effect=RuntimeError("channel down"))

        # Should not raise
        deliver(
            delivery=job.delivery,
            output="hello",
            job=job,
            channel_send=channel_send,
        )

    def test_announce_failure_not_best_effort_raises(self):
        """If channel_send raises and best_effort is False, exception propagates."""
        job = _make_job(
            delivery_mode="announce",
            channel="whatsapp",
            best_effort=False,
        )
        channel_send = MagicMock(side_effect=RuntimeError("channel down"))

        with pytest.raises(RuntimeError, match="channel down"):
            deliver(
                delivery=job.delivery,
                output="hello",
                job=job,
                channel_send=channel_send,
            )


# -- Webhook delivery --


class TestDeliverWebhook:
    @patch("httpx.post")
    def test_webhook_posts_json(self, mock_post):
        """Webhook mode should POST job output as JSON to the URL."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        job = _make_job(
            delivery_mode="webhook",
            url="https://hooks.example.com/notify",
        )

        deliver(
            delivery=job.delivery,
            output="Job completed successfully",
            job=job,
        )

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://hooks.example.com/notify"
        payload = call_args[1]["json"]
        assert payload["job_id"] == job.id
        assert payload["job_name"] == "test job"
        assert payload["output"] == "Job completed successfully"

    @patch("httpx.post")
    def test_webhook_calls_raise_for_status(self, mock_post):
        """Webhook should call raise_for_status() on the response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        job = _make_job(
            delivery_mode="webhook",
            url="https://hooks.example.com/notify",
        )

        deliver(delivery=job.delivery, output="ok", job=job)

        mock_response.raise_for_status.assert_called_once()

    @patch("httpx.post")
    def test_webhook_failure_best_effort_swallowed(self, mock_post):
        """If webhook POST fails and best_effort is True, no exception propagates."""
        mock_post.side_effect = ConnectionError("network error")

        job = _make_job(
            delivery_mode="webhook",
            url="https://hooks.example.com/notify",
            best_effort=True,
        )

        # Should not raise
        deliver(delivery=job.delivery, output="hello", job=job)

    @patch("httpx.post")
    def test_webhook_failure_not_best_effort_raises(self, mock_post):
        """If webhook POST fails and best_effort is False, exception propagates."""
        mock_post.side_effect = ConnectionError("network error")

        job = _make_job(
            delivery_mode="webhook",
            url="https://hooks.example.com/notify",
            best_effort=False,
        )

        with pytest.raises(ConnectionError, match="network error"):
            deliver(delivery=job.delivery, output="hello", job=job)

    @patch("httpx.post")
    def test_webhook_timeout(self, mock_post):
        """Webhook POST should use a 30-second timeout."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        job = _make_job(
            delivery_mode="webhook",
            url="https://hooks.example.com/notify",
        )

        deliver(delivery=job.delivery, output="ok", job=job)

        call_args = mock_post.call_args
        assert call_args[1]["timeout"] == 30


# -- Best effort flag --


class TestBestEffort:
    def test_best_effort_default_is_true(self):
        """The Delivery model default for best_effort should be True."""
        d = Delivery(mode="none")
        assert d.best_effort is True

    @patch("httpx.post")
    def test_best_effort_true_swallows_webhook_error(self, mock_post):
        """best_effort=True should swallow delivery errors."""
        mock_post.side_effect = RuntimeError("boom")

        job = _make_job(
            delivery_mode="webhook",
            url="https://hooks.example.com/notify",
            best_effort=True,
        )

        # Should not raise
        deliver(delivery=job.delivery, output="hello", job=job)

    def test_best_effort_true_swallows_announce_error(self):
        """best_effort=True should swallow announce delivery errors."""
        job = _make_job(
            delivery_mode="announce",
            channel="whatsapp",
            best_effort=True,
        )
        channel_send = MagicMock(side_effect=RuntimeError("broken"))

        # Should not raise
        deliver(
            delivery=job.delivery,
            output="hello",
            job=job,
            channel_send=channel_send,
        )
