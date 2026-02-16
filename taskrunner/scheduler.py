"""Scheduler - runs tasks on cron schedules using APScheduler."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from taskrunner.models import load_all_tasks
from taskrunner.orchestrator import run_task

logger = logging.getLogger(__name__)


def start_scheduler(
    tasks_dir: str | Path = "tasks",
    use_containers: bool = False,
    shutdown_event: "threading.Event | None" = None,
    on_failure: Callable[[str, Exception], None] | None = None,
    heartbeat_event: "threading.Event | None" = None,
    heartbeat_interval: int = 30,
) -> BlockingScheduler:
    """Load all tasks and start the blocking scheduler.

    Args:
        tasks_dir: Directory containing task YAML files.
        use_containers: Whether to run executors/LLM in Docker containers.
        shutdown_event: Optional threading.Event; when set, shuts down the scheduler.
        on_failure: Optional callback invoked with (task_path, exception) on task failure.
        heartbeat_event: Optional threading.Event; set periodically to signal liveness.
        heartbeat_interval: Seconds between heartbeat signals (default 30).

    Returns:
        The scheduler instance (useful for external shutdown).
    """
    import threading

    tasks_dir = Path(tasks_dir)
    tasks = load_all_tasks(tasks_dir)

    if not tasks:
        logger.warning("No tasks found in %s", tasks_dir)
        return BlockingScheduler()

    scheduler = BlockingScheduler()

    for task in tasks:
        task_file = tasks_dir / f"{task.name}.yaml"
        logger.info(
            "Scheduling task '%s' with cron: %s", task.name, task.schedule
        )
        scheduler.add_job(
            _run_task_safe,
            CronTrigger.from_crontab(task.schedule),
            args=[str(task_file), use_containers, on_failure],
            id=task.name,
            name=task.name,
        )

    # If a shutdown event is provided, watch it in a background thread
    if shutdown_event is not None:
        def _watch_shutdown():
            shutdown_event.wait()
            logger.info("Shutdown event received, stopping scheduler")
            scheduler.shutdown(wait=False)

        watcher = threading.Thread(target=_watch_shutdown, daemon=True)
        watcher.start()

    # If a heartbeat event is provided, pulse it periodically in a daemon thread
    if heartbeat_event is not None:
        def _heartbeat_loop():
            while True:
                heartbeat_event.set()
                if shutdown_event is not None and shutdown_event.wait(heartbeat_interval):
                    break
                elif shutdown_event is None:
                    threading.Event().wait(heartbeat_interval)

        hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        hb_thread.start()

    logger.info("Starting scheduler with %d tasks", len(tasks))

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")
        scheduler.shutdown()

    return scheduler


def _run_task_safe(
    task_path: str,
    use_containers: bool,
    on_failure: Callable[[str, Exception], None] | None = None,
) -> None:
    """Run a task with error handling so the scheduler doesn't crash."""
    try:
        result = run_task(task_path, use_containers=use_containers)
        logger.info("Task %s completed successfully (%d chars)", task_path, len(result))
    except Exception as exc:
        logger.exception("Task %s failed", task_path)
        if on_failure is not None:
            try:
                on_failure(task_path, exc)
            except Exception:
                logger.exception("on_failure callback raised for task %s", task_path)
