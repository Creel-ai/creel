"""Tests for the scheduler."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from taskrunner.scheduler import _run_task_safe, start_scheduler


def _make_tasks_dir(tmp_path: Path, tasks: list[dict] | None = None) -> Path:
    """Create a tasks directory with YAML files."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    if tasks:
        for task in tasks:
            path = tasks_dir / f"{task['name']}.yaml"
            path.write_text(yaml.dump(task))
    return tasks_dir


def _sample_task(name: str = "test_task", schedule: str = "0 7 * * *") -> dict:
    return {
        "name": name,
        "schedule": schedule,
        "executors": {"weather": {"args": {"location": "denver"}}},
        "prompt": "Weather: {weather}",
        "output": {"type": "stdout", "to": ""},
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 100},
    }


# --- start_scheduler tests ---


@patch("taskrunner.scheduler.run_task")
def test_start_scheduler_loads_and_schedules_tasks(mock_run_task, tmp_path):
    """Tasks loaded from YAML, add_job called per task, scheduler.start() called."""
    tasks_dir = _make_tasks_dir(
        tmp_path, [_sample_task("alpha"), _sample_task("beta", "30 8 * * *")]
    )

    with patch("apscheduler.schedulers.blocking.BlockingScheduler.start"):
        scheduler = start_scheduler(tasks_dir=tasks_dir)

    jobs = scheduler.get_jobs()
    job_names = {j.name for j in jobs}
    assert "alpha" in job_names
    assert "beta" in job_names
    assert len(jobs) == 2


@patch("taskrunner.scheduler.run_task")
def test_start_scheduler_no_tasks(mock_run_task, tmp_path):
    """Empty dir logs warning and returns empty scheduler."""
    tasks_dir = _make_tasks_dir(tmp_path, [])

    scheduler = start_scheduler(tasks_dir=tasks_dir)

    assert scheduler.get_jobs() == []


@patch("taskrunner.scheduler.run_task")
def test_shutdown_event_stops_scheduler(mock_run_task, tmp_path):
    """Setting event triggers scheduler.shutdown(wait=False)."""
    tasks_dir = _make_tasks_dir(tmp_path, [_sample_task()])
    shutdown_event = threading.Event()

    with (
        patch("apscheduler.schedulers.blocking.BlockingScheduler.start") as mock_start,
        patch("apscheduler.schedulers.blocking.BlockingScheduler.shutdown") as mock_shutdown,
    ):
        # Simulate start() blocking until shutdown is called
        def _block_until_shutdown():
            shutdown_event.wait(timeout=2)

        mock_start.side_effect = _block_until_shutdown

        def _run():
            start_scheduler(tasks_dir=tasks_dir, shutdown_event=shutdown_event)

        t = threading.Thread(target=_run)
        t.start()

        # Give the scheduler thread time to start, then trigger shutdown
        shutdown_event.set()
        t.join(timeout=5)

        assert not t.is_alive()
        mock_shutdown.assert_called_once_with(wait=False)


@patch("taskrunner.scheduler.run_task")
def test_keyboard_interrupt_stops_scheduler(mock_run_task, tmp_path):
    """start() raising KeyboardInterrupt calls shutdown()."""
    tasks_dir = _make_tasks_dir(tmp_path, [_sample_task()])

    with (
        patch(
            "apscheduler.schedulers.blocking.BlockingScheduler.start",
            side_effect=KeyboardInterrupt,
        ),
        patch("apscheduler.schedulers.blocking.BlockingScheduler.shutdown") as mock_shutdown,
    ):
        start_scheduler(tasks_dir=tasks_dir)

    mock_shutdown.assert_called_once()


# --- _run_task_safe tests ---


@patch("taskrunner.scheduler.run_task", return_value="ok result")
def test_run_task_safe_success(mock_run_task):
    """Calls run_task, logs success."""
    _run_task_safe("tasks/test.yaml", False)
    mock_run_task.assert_called_once_with("tasks/test.yaml", use_containers=False)


@patch("taskrunner.scheduler.run_task", side_effect=RuntimeError("boom"))
def test_run_task_safe_failure(mock_run_task):
    """Exception caught, logged, no re-raise."""
    _run_task_safe("tasks/test.yaml", False)
    # Should not raise


@patch("taskrunner.scheduler.run_task", side_effect=RuntimeError("boom"))
def test_run_task_safe_calls_failure_callback(mock_run_task):
    """Callback invoked with task path + exception."""
    callback = MagicMock()
    _run_task_safe("tasks/test.yaml", False, on_failure=callback)

    callback.assert_called_once()
    args = callback.call_args[0]
    assert args[0] == "tasks/test.yaml"
    assert isinstance(args[1], RuntimeError)


@patch("taskrunner.scheduler.run_task", return_value="ok")
def test_run_task_safe_no_callback_on_success(mock_run_task):
    """Callback not called on success."""
    callback = MagicMock()
    _run_task_safe("tasks/test.yaml", False, on_failure=callback)
    callback.assert_not_called()


@patch("taskrunner.scheduler.run_task", side_effect=RuntimeError("boom"))
def test_failure_callback_exception_is_caught(mock_run_task):
    """Broken callback doesn't crash scheduler."""
    callback = MagicMock(side_effect=ValueError("callback broke"))
    # Should not raise despite callback raising
    _run_task_safe("tasks/test.yaml", False, on_failure=callback)


# --- heartbeat tests ---


@patch("taskrunner.scheduler.run_task")
def test_heartbeat_event_set_periodically(mock_run_task, tmp_path):
    """Heartbeat set at least once during short run."""
    tasks_dir = _make_tasks_dir(tmp_path, [_sample_task()])
    heartbeat_event = threading.Event()
    shutdown_event = threading.Event()

    with patch("apscheduler.schedulers.blocking.BlockingScheduler.start") as mock_start:
        # Let start() block briefly then return
        mock_start.side_effect = lambda: shutdown_event.wait(0.5)

        start_scheduler(
            tasks_dir=tasks_dir,
            heartbeat_event=heartbeat_event,
            heartbeat_interval=1,
            shutdown_event=shutdown_event,
        )

    assert heartbeat_event.is_set()


@patch("taskrunner.scheduler.run_task")
def test_heartbeat_without_event(mock_run_task, tmp_path):
    """Scheduler works fine without heartbeat."""
    tasks_dir = _make_tasks_dir(tmp_path, [_sample_task()])

    with patch("apscheduler.schedulers.blocking.BlockingScheduler.start"):
        scheduler = start_scheduler(tasks_dir=tasks_dir)

    assert len(scheduler.get_jobs()) == 1
