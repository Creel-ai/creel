"""JSON file persistence for monitors and alert history."""

from __future__ import annotations

import builtins
import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from creel import paths
from creel.monitors.models import AlertRecord, Monitor, MonitorRunRecord

logger = logging.getLogger(__name__)

DEFAULT_MAX_RUNS_PER_MONITOR = 50
DEFAULT_MAX_ALERTS_PER_MONITOR = 100


class MonitorStore:
    """Persistent storage for monitors, run history, and alert records.

    Mirrors the cron JobStore pattern — JSON files with thread-safe access.
    """

    def __init__(
        self,
        monitors_path: Path | str | None = None,
        runs_path: Path | str | None = None,
        alerts_path: Path | str | None = None,
        max_runs_per_monitor: int = DEFAULT_MAX_RUNS_PER_MONITOR,
        max_alerts_per_monitor: int = DEFAULT_MAX_ALERTS_PER_MONITOR,
    ) -> None:
        if monitors_path is None:
            monitors_path = paths.monitors_dir() / "monitors.json"
        if runs_path is None:
            runs_path = paths.monitors_dir() / "runs.json"
        if alerts_path is None:
            alerts_path = paths.monitors_dir() / "alerts.json"
        self._monitors_path = Path(monitors_path)
        self._runs_path = Path(runs_path)
        self._alerts_path = Path(alerts_path)
        self._max_runs = max_runs_per_monitor
        self._max_alerts = max_alerts_per_monitor
        self._lock = threading.Lock()

        self._monitors: dict[str, Monitor] = {}
        self._runs: dict[str, list[MonitorRunRecord]] = {}
        self._alerts: dict[str, list[AlertRecord]] = {}

        self.load()

    # -- Public API: monitors --

    def add(self, monitor: Monitor) -> Monitor:
        """Add a new monitor. Raises ValueError if ID already exists."""
        with self._lock:
            if monitor.id in self._monitors:
                raise ValueError(f"Monitor with id '{monitor.id}' already exists")
            self._monitors[monitor.id] = monitor
            self._save_monitors()
            return monitor

    def get(self, monitor_id: str) -> Monitor | None:
        """Get a monitor by ID, or None if not found."""
        with self._lock:
            return self._monitors.get(monitor_id)

    def list(self) -> builtins.list[Monitor]:
        """Return all monitors, ordered by created_at."""
        with self._lock:
            return sorted(self._monitors.values(), key=lambda m: m.created_at)

    def update(self, monitor_id: str, **fields: object) -> Monitor:
        """Update fields on an existing monitor. Returns the updated monitor."""
        with self._lock:
            monitor = self._monitors.get(monitor_id)
            if monitor is None:
                raise KeyError(f"Monitor '{monitor_id}' not found")

            data = monitor.model_dump()
            for key, value in fields.items():
                if key == "id":
                    raise ValueError("Cannot change monitor ID")
                if key not in data:
                    raise ValueError(f"Unknown field '{key}'")
                data[key] = value

            from creel.monitors.models import now_iso

            data["updated_at"] = now_iso()
            updated = Monitor(**data)
            self._monitors[monitor_id] = updated
            self._save_monitors()
            return updated

    def remove(self, monitor_id: str, *, keep_history: bool = False) -> Monitor:
        """Remove a monitor by ID. Returns the removed monitor."""
        with self._lock:
            monitor = self._monitors.pop(monitor_id, None)
            if monitor is None:
                raise KeyError(f"Monitor '{monitor_id}' not found")
            self._save_monitors()
            if not keep_history:
                self._runs.pop(monitor_id, None)
                self._save_runs()
                self._alerts.pop(monitor_id, None)
                self._save_alerts()
            return monitor

    # -- Public API: run history --

    def add_run(self, record: MonitorRunRecord) -> MonitorRunRecord:
        """Append a run record, capping history."""
        with self._lock:
            runs = self._runs.setdefault(record.monitor_id, [])
            runs.append(record)
            if len(runs) > self._max_runs:
                self._runs[record.monitor_id] = runs[-self._max_runs :]
            self._save_runs()
            return record

    def get_runs(self, monitor_id: str) -> builtins.list[MonitorRunRecord]:
        """Get run history for a monitor, oldest first."""
        with self._lock:
            return list(self._runs.get(monitor_id, []))

    # -- Public API: alert records --

    def add_alert(self, alert: AlertRecord) -> AlertRecord:
        """Append an alert record, capping history."""
        with self._lock:
            alerts = self._alerts.setdefault(alert.monitor_id, [])
            alerts.append(alert)
            if len(alerts) > self._max_alerts:
                self._alerts[alert.monitor_id] = alerts[-self._max_alerts :]
            self._save_alerts()
            return alert

    def get_alerts(self, monitor_id: str) -> builtins.list[AlertRecord]:
        """Get alert history for a monitor, oldest first."""
        with self._lock:
            return list(self._alerts.get(monitor_id, []))

    def get_recent_fingerprints(self, monitor_id: str, since: datetime) -> builtins.list[str]:
        """Return fingerprints of alerts sent since the given time.

        Used for deduplication — if a fingerprint is in this list,
        the alert is within its cooldown period.
        """
        with self._lock:
            alerts = self._alerts.get(monitor_id, [])
            result = []
            for a in alerts:
                if not a.delivered:
                    continue
                try:
                    ts = datetime.fromisoformat(a.timestamp)
                    if ts >= since:
                        result.append(a.fingerprint)
                except ValueError:
                    continue
            return result

    # -- Persistence --

    def load(self) -> None:
        """Load monitors, runs, and alerts from disk."""
        with self._lock:
            self._monitors = {}
            self._runs = {}
            self._alerts = {}
            self._load_monitors()
            self._load_runs()
            self._load_alerts()

    def save(self) -> None:
        """Write all data to disk."""
        with self._lock:
            self._save_monitors()
            self._save_runs()
            self._save_alerts()

    def _load_monitors(self) -> None:
        if not self._monitors_path.exists():
            return
        try:
            data = json.loads(self._monitors_path.read_text())
            for item in data:
                m = Monitor(**item)
                self._monitors[m.id] = m
        except Exception:
            logger.exception(
                "Failed to load monitors from %s — starting empty",
                self._monitors_path,
            )

    def _load_runs(self) -> None:
        if not self._runs_path.exists():
            return
        try:
            data = json.loads(self._runs_path.read_text())
            for mid, records in data.items():
                self._runs[mid] = [MonitorRunRecord(**r) for r in records]
        except Exception:
            logger.exception(
                "Failed to load monitor runs from %s — starting empty",
                self._runs_path,
            )

    def _load_alerts(self) -> None:
        if not self._alerts_path.exists():
            return
        try:
            data = json.loads(self._alerts_path.read_text())
            for mid, records in data.items():
                self._alerts[mid] = [AlertRecord(**r) for r in records]
        except Exception:
            logger.exception(
                "Failed to load alerts from %s — starting empty",
                self._alerts_path,
            )

    def _save_monitors(self) -> None:
        self._monitors_path.parent.mkdir(parents=True, exist_ok=True)
        data = [m.model_dump() for m in self._monitors.values()]
        self._atomic_write(self._monitors_path, json.dumps(data, indent=2) + "\n")

    def _save_runs(self) -> None:
        self._runs_path.parent.mkdir(parents=True, exist_ok=True)
        data = {mid: [r.model_dump() for r in records] for mid, records in self._runs.items()}
        self._atomic_write(self._runs_path, json.dumps(data, indent=2) + "\n")

    def _save_alerts(self) -> None:
        self._alerts_path.parent.mkdir(parents=True, exist_ok=True)
        data = {mid: [a.model_dump() for a in records] for mid, records in self._alerts.items()}
        self._atomic_write(self._alerts_path, json.dumps(data, indent=2) + "\n")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write content to a file atomically via temp file + rename."""
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
