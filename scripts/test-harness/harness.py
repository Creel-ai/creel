"""Test harness orchestrator for Creel integration tests.

Manages the lifecycle of the mock LLM server and the Creel daemon,
runs pytest scenarios, and generates a JSON test report.

Usage:
    python scripts/test-harness/harness.py [--scenarios-dir DIR] [--report FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parent.parent
CONFIG_DIR = HARNESS_DIR / "config"
SCENARIOS_DIR = HARNESS_DIR / "scenarios"
RESULTS_DIR = REPO_ROOT / "test-results"

MOCK_LLM_PORT = 18999
DAEMON_SOCKET = HARNESS_DIR / "run" / "daemon.sock"
DAEMON_PID_FILE = HARNESS_DIR / "run" / "daemon.pid"


def _wait_for_url(url: str, timeout: float = 10.0, interval: float = 0.3) -> bool:
    """Poll a URL until it returns 200 or timeout."""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=1.0)
            if resp.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
            pass
        time.sleep(interval)
    return False


def _wait_for_uds(socket_path: Path, timeout: float = 15.0, interval: float = 0.3) -> bool:
    """Poll a Unix domain socket until the daemon responds to /health."""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            try:
                transport = httpx.HTTPTransport(uds=str(socket_path))
                with httpx.Client(transport=transport, base_url="http://daemon", timeout=1.0) as client:
                    resp = client.get("/health")
                    if resp.status_code == 200:
                        return True
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException, OSError):
                pass
        time.sleep(interval)
    return False


def _kill_process(proc: subprocess.Popen, label: str, timeout: float = 5.0) -> None:
    """Gracefully stop a subprocess, escalating to SIGKILL if needed."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"  {label}: SIGTERM timed out, sending SIGKILL")
        proc.kill()
        proc.wait(timeout=3)


class TestHarness:
    """Orchestrates mock LLM server, Creel daemon, and pytest scenarios."""

    def __init__(
        self,
        scenarios_dir: Path = SCENARIOS_DIR,
        results_dir: Path = RESULTS_DIR,
    ):
        self.scenarios_dir = scenarios_dir
        self.results_dir = results_dir
        self.mock_proc: subprocess.Popen | None = None
        self.daemon_proc: subprocess.Popen | None = None
        self._start_time: float = 0

    def setup(self) -> bool:
        """Start mock LLM server and Creel daemon. Returns True if both are healthy."""
        self._start_time = time.monotonic()
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / "logs").mkdir(exist_ok=True)

        # Ensure run directory for daemon socket
        run_dir = HARNESS_DIR / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        DAEMON_SOCKET.unlink(missing_ok=True)
        DAEMON_PID_FILE.unlink(missing_ok=True)

        # 1. Start mock LLM server
        print("Starting mock LLM server...")
        log_mock = open(self.results_dir / "logs" / "mock-llm.log", "w")
        self.mock_proc = subprocess.Popen(
            [
                sys.executable,
                str(HARNESS_DIR / "mock_llm_server.py"),
                "--port", str(MOCK_LLM_PORT),
            ],
            stdout=log_mock,
            stderr=subprocess.STDOUT,
            cwd=str(REPO_ROOT),
        )

        mock_url = f"http://127.0.0.1:{MOCK_LLM_PORT}/health"
        if not _wait_for_url(mock_url):
            print("ERROR: Mock LLM server failed to start")
            return False
        print(f"  Mock LLM server healthy on port {MOCK_LLM_PORT}")

        # 2. Start Creel daemon
        print("Starting Creel daemon...")
        log_daemon = open(self.results_dir / "logs" / "daemon.log", "w")
        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = "test-key-not-real"
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{MOCK_LLM_PORT}"

        self.daemon_proc = subprocess.Popen(
            [
                sys.executable, "-m", "taskrunner",
                "--agent-config", str(CONFIG_DIR / "agent.yaml"),
                "--no-judge",
                "daemon", "run",
                "--socket-path", str(DAEMON_SOCKET),
                "--pid-file", str(DAEMON_PID_FILE),
                "--channel", "telegram",
                "--no-scheduler",
            ],
            stdout=log_daemon,
            stderr=subprocess.STDOUT,
            cwd=str(CONFIG_DIR),  # CWD = config dir so relative paths resolve
            env=env,
        )

        if not _wait_for_uds(DAEMON_SOCKET):
            print("ERROR: Creel daemon failed to start")
            # Dump daemon log for debugging
            daemon_log_path = self.results_dir / "logs" / "daemon.log"
            if daemon_log_path.exists():
                print("--- daemon.log ---")
                print(daemon_log_path.read_text()[-2000:])
                print("--- end ---")
            return False
        print(f"  Creel daemon healthy on socket {DAEMON_SOCKET}")

        return True

    def teardown(self) -> None:
        """Stop daemon and mock LLM server, clean up test artifacts."""
        print("Cleaning up...")

        if self.daemon_proc:
            _kill_process(self.daemon_proc, "daemon")
        if self.mock_proc:
            _kill_process(self.mock_proc, "mock-llm")

        # Clean up test data directories
        for subdir in ("sessions", "workspace", "media", "approvals"):
            d = CONFIG_DIR / subdir
            if d.exists():
                import shutil
                shutil.rmtree(d, ignore_errors=True)

        # Clean up daemon runtime files
        DAEMON_SOCKET.unlink(missing_ok=True)
        DAEMON_PID_FILE.unlink(missing_ok=True)

        # Clean up audit log
        audit_log = CONFIG_DIR / "guardian_audit.jsonl"
        audit_log.unlink(missing_ok=True)

    def run_scenarios(self) -> int:
        """Run pytest on the scenarios directory. Returns exit code."""
        if not self.scenarios_dir.exists():
            print(f"No scenarios directory at {self.scenarios_dir}, smoke test only.")
            return 0

        scenario_files = list(self.scenarios_dir.glob("test_*.py"))
        if not scenario_files:
            print("No test_*.py files in scenarios/, smoke test only.")
            return 0

        print(f"Running {len(scenario_files)} scenario file(s)...")
        env = os.environ.copy()
        env["HARNESS_DAEMON_SOCKET"] = str(DAEMON_SOCKET)
        env["HARNESS_MOCK_LLM_URL"] = f"http://127.0.0.1:{MOCK_LLM_PORT}"

        report_file = self.results_dir / "pytest-report.json"
        cmd = [
            sys.executable, "-m", "pytest",
            str(self.scenarios_dir),
            "--tb=short",
            "-q",
        ]

        log_path = self.results_dir / "logs" / "test-output.log"
        with open(log_path, "w") as log_file:
            result = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            log_file.write(result.stdout)
            print(result.stdout)

        return result.returncode

    def generate_report(self, exit_code: int) -> dict:
        """Generate test-results/report.json with summary."""
        duration = time.monotonic() - self._start_time

        # Parse pytest output for test counts
        log_path = self.results_dir / "logs" / "test-output.log"
        total = passed = failed = errors = 0
        if log_path.exists():
            output = log_path.read_text()
            # Parse pytest summary line like "5 passed, 2 failed in 1.23s"
            import re
            m = re.search(r"(\d+) passed", output)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+) failed", output)
            if m:
                failed = int(m.group(1))
            m = re.search(r"(\d+) error", output)
            if m:
                errors = int(m.group(1))
            total = passed + failed + errors

        report = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_seconds": round(duration, 1),
            "exit_code": exit_code,
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
        }

        report_path = self.results_dir / "report.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Report written to {report_path}")
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Creel Integration Test Harness")
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=SCENARIOS_DIR,
        help=f"Path to scenario test files (default: {SCENARIOS_DIR})",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS_DIR,
        help=f"Path for results output (default: {RESULTS_DIR})",
    )
    args = parser.parse_args()

    harness = TestHarness(
        scenarios_dir=args.scenarios_dir,
        results_dir=args.results_dir,
    )

    # Handle signals for clean shutdown
    def _signal_handler(signum, frame):
        print(f"\nReceived signal {signum}, cleaning up...")
        harness.teardown()
        sys.exit(1)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print("=== Creel Integration Test Harness ===\n")

    try:
        if not harness.setup():
            harness.teardown()
            return 1

        exit_code = harness.run_scenarios()
        report = harness.generate_report(exit_code)

        print()
        if exit_code == 0:
            print(f"All integration tests passed! ({report['passed']} tests)")
        else:
            print(f"Some tests failed. {report['passed']} passed, {report['failed']} failed, {report['errors']} errors")

        return exit_code
    finally:
        harness.teardown()


if __name__ == "__main__":
    sys.exit(main())
