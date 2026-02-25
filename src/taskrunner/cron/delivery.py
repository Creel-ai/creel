"""Delivery routing — sends isolated job output to channels, webhooks, or nowhere."""

from __future__ import annotations

import logging

from taskrunner.cron.models import ChannelSendFn, CronJob, Delivery

logger = logging.getLogger(__name__)


def deliver(
    delivery: Delivery,
    output: str,
    job: CronJob,
    channel_send: ChannelSendFn | None = None,
) -> None:
    """Route job output according to the delivery configuration.

    Args:
        delivery: Delivery settings (mode, channel, url, best_effort).
        output: The text output from the job execution.
        job: The job that produced the output (for context in webhook payloads).
        channel_send: Callback to send to a named channel (required for announce mode).

    Raises:
        RuntimeError: If delivery mode requires a dependency that isn't configured.
        Exception: If delivery fails and best_effort is False.
    """
    if delivery.mode == "none":
        logger.debug("Delivery mode is 'none' for job '%s' — skipping", job.name)
        return

    try:
        if delivery.mode == "announce":
            _deliver_announce(delivery, output, job, channel_send)
        elif delivery.mode == "webhook":
            _deliver_webhook(delivery, output, job)
        else:
            raise ValueError(f"Unknown delivery mode: {delivery.mode}")
    except Exception:
        if delivery.best_effort:
            logger.exception(
                "Delivery failed for job '%s' (%s) — best_effort=True, continuing",
                job.name,
                job.id,
            )
        else:
            raise


def _deliver_announce(
    delivery: Delivery,
    output: str,
    job: CronJob,
    channel_send: ChannelSendFn | None,
) -> None:
    """Send output to a chat channel."""
    if channel_send is None:
        raise RuntimeError(
            f"Cannot deliver to channel '{delivery.channel}': "
            "no channel_send callback configured"
        )
    channel_send(delivery.channel, output)
    logger.info(
        "Delivered output for job '%s' to channel '%s'",
        job.name,
        delivery.channel,
    )


def _deliver_webhook(
    delivery: Delivery,
    output: str,
    job: CronJob,
) -> None:
    """POST output to a webhook URL."""
    import httpx

    payload = {
        "job_id": job.id,
        "job_name": job.name,
        "output": output,
    }
    response = httpx.post(
        delivery.url, json=payload, timeout=30, follow_redirects=False
    )
    response.raise_for_status()
    logger.info(
        "Delivered output for job '%s' to webhook %s (status=%d)",
        job.name,
        delivery.url,
        response.status_code,
    )
