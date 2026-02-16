#!/usr/bin/env python3
"""LLM Task Runner - CLI entry point.

Usage:
    creel run <task_name>            Run a task immediately
    creel run <task_name> --dry      Dry run (render prompt only)
    creel attach                     Attach TUI to running daemon
    creel schedule                   Start scheduler for all tasks
    creel daemon start               Start daemon in background
    creel daemon stop                Stop daemon
    creel daemon status              Show daemon status
    creel send "message"             Send one message via daemon API
"""

from __future__ import annotations

# Use OS certificate store for HTTPS (fixes uv-managed Python SSL issues)
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

from taskrunner.models import load_all_tasks, load_task
from taskrunner.orchestrator import run_task
from taskrunner.secrets import parse_env_file
from taskrunner.scheduler import start_scheduler

DEFAULT_TASKS_DIR = Path("tasks")
DEFAULT_AGENT_CONFIG = Path("agent.yaml")
DEFAULT_DAEMON_SOCKET = Path("/tmp/creel-daemon.sock")
DEFAULT_DAEMON_PID_FILE = Path("/tmp/creel-daemon.pid")
DEFAULT_DAEMON_LOG_FILE = Path("/tmp/creel-daemon.log")


def _tasks_dir(args: argparse.Namespace) -> Path:
    return args.tasks_dir


def _daemon_socket_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "socket_path", DEFAULT_DAEMON_SOCKET))


def _daemon_pid_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "pid_file", DEFAULT_DAEMON_PID_FILE))


def _daemon_log_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "log_file", DEFAULT_DAEMON_LOG_FILE))


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_is_running(pid: int) -> bool:
    import errno

    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _daemon_request(
    socket_path: Path,
    method: str,
    url_path: str,
    json_body: dict | None = None,
    timeout: float = 5.0,
):
    import httpx

    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(
        transport=transport,
        base_url="http://daemon",
        timeout=timeout,
    ) as client:
        return client.request(method, url_path, json=json_body)


def _cleanup_stale_daemon_files(pid_path: Path, socket_path: Path) -> None:
    pid_path.unlink(missing_ok=True)
    socket_path.unlink(missing_ok=True)


def cmd_run(args: argparse.Namespace) -> int:
    """Run a single task immediately."""
    tasks_dir = _tasks_dir(args)
    task_file = tasks_dir / f"{args.task_name}.yaml"
    if not task_file.exists():
        print(f"Error: Task '{args.task_name}' not found at {task_file}", file=sys.stderr)
        return 1

    try:
        result = run_task(
            task_file,
            use_containers=args.containers,
            dry_run=args.dry,
        )
        if args.dry:
            print("=== Rendered Prompt ===")
            print(result)
        else:
            print(f"Task '{args.task_name}' completed.")
            if args.verbose:
                print("=== Output ===")
                print(result)
    except Exception as e:
        print(f"Error running task '{args.task_name}': {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1

    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    """Start the scheduler."""
    print("Starting scheduler...")
    try:
        start_scheduler(
            tasks_dir=_tasks_dir(args),
            use_containers=args.containers,
        )
    except KeyboardInterrupt:
        print("\nScheduler stopped.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List all available tasks."""
    tasks_dir = _tasks_dir(args)
    try:
        tasks = load_all_tasks(tasks_dir)
    except FileNotFoundError:
        print(f"Tasks directory not found: {tasks_dir}", file=sys.stderr)
        return 1

    if not tasks:
        print("No tasks found.")
        return 0

    print(f"{'Name':<25} {'Schedule':<20} {'Executors':<30} {'Output'}")
    print("-" * 90)
    for task in tasks:
        executors_list = ", ".join(task.executors.keys())
        print(f"{task.name:<25} {task.schedule:<20} {executors_list:<30} {task.output.type}:{task.output.to}")

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a task definition."""
    tasks_dir = _tasks_dir(args)
    task_file = tasks_dir / f"{args.task_name}.yaml"
    if not task_file.exists():
        print(f"Error: Task '{args.task_name}' not found at {task_file}", file=sys.stderr)
        return 1

    try:
        task = load_task(task_file)
        print(f"Task '{task.name}' is valid.")
        print(f"  Schedule: {task.schedule}")
        print(f"  Executors: {', '.join(task.executors.keys())}")
        print(f"  Output: {task.output.type} -> {task.output.to}")
        print(f"  LLM: {task.llm.model} (max {task.llm.max_tokens} tokens)")
        return 0
    except Exception as e:
        print(f"Validation failed: {e}", file=sys.stderr)
        return 1


def _load_agent_def(args: argparse.Namespace):
    """Load the global agent definition from agent.yaml."""
    from taskrunner.models import load_agent_config

    config_path = args.agent_config
    agent_def = load_agent_config(config_path)

    # CLI overrides
    if getattr(args, "no_judge", False) and agent_def.guardian:
        agent_def.guardian.llm_judge.enabled = False
        logger.info("LLM judge disabled via --no-judge flag")

    return agent_def


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach Textual TUI to a running daemon."""
    from taskrunner.daemon.client import DaemonApiClient, DaemonTuiAdapter
    from taskrunner.tui import ChatApp

    socket_path = _daemon_socket_path(args)
    sender_id = getattr(args, "sender_id", "cli")
    timeout = max(1.0, getattr(args, "timeout", 300.0))

    client = DaemonApiClient(socket_path=socket_path, timeout=timeout)
    try:
        client.health()
    except Exception as e:
        print(f"Error: daemon is unreachable at {socket_path}: {e}", file=sys.stderr)
        return 1

    # Suppress console log handlers — Textual owns the terminal
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if isinstance(handler, logging.StreamHandler):
            root.removeHandler(handler)

    backend = DaemonTuiAdapter(client, sender_id=sender_id)

    if args.new:
        backend.new_session(sender_id)
    if args.resume:
        try:
            backend.resume_session(sender_id, args.resume)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    app = ChatApp(backend, sender_id=sender_id)
    app.run()
    return 0


def _build_daemon_channel(agent_def, channel_type: str):
    """Build an optional channel plugin for daemon runtime."""
    if channel_type == "none":
        return None, None

    if channel_type == "bluebubbles":
        from taskrunner.channels.bluebubbles import BlueBubblesChannel

        bb_cfg = agent_def.channels.bluebubbles
        if not bb_cfg:
            raise ValueError("No bluebubbles channel configured in agent.yaml")

        channel = BlueBubblesChannel(
            server_url=bb_cfg.server_url,
            password=bb_cfg.password,
            allowed_senders=bb_cfg.listen_to,
            poll_interval=bb_cfg.poll_interval,
        )
        return channel, None

    from taskrunner.channels.imessage import IMessageChannel

    if not agent_def.channels.imessage:
        raise ValueError("No imessage channel configured in agent.yaml")
    channel = IMessageChannel(
        allowed_senders=[agent_def.channels.imessage.listen_to],
        poll_interval=agent_def.channels.imessage.poll_interval,
    )
    return channel, channel


def cmd_daemon_run(args: argparse.Namespace) -> int:
    """Run the daemon server in the foreground (internal command)."""
    import uvicorn

    from taskrunner.chat import ChatServer
    from taskrunner.daemon.api import create_daemon_app
    from taskrunner.daemon.service import DaemonService
    from taskrunner.startup import SecretsValidationError, validate_secrets

    try:
        agent_def = _load_agent_def(args)
    except FileNotFoundError:
        print(f"Error: Agent config not found at {args.agent_config}", file=sys.stderr)
        return 1

    try:
        validate_secrets(agent_def)
    except SecretsValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if agent_def.llm.secrets:
        from taskrunner.orchestrator import _load_secrets_to_env

        _load_secrets_to_env(agent_def.llm.secrets)

    channel_type = getattr(args, "channel_type", "imessage")
    try:
        channel, imessage_channel = _build_daemon_channel(agent_def, channel_type)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    server = ChatServer(
        agent_def,
        use_containers=args.containers,
        imessage_channel=imessage_channel,
    )
    service = DaemonService(
        agent_def,
        use_containers=args.containers,
        server=server,
    )

    if not getattr(args, "no_scheduler", False):
        tasks_dir = _tasks_dir(args)
        if tasks_dir.is_dir():
            service.start_scheduler(tasks_dir)

    if channel is not None:
        service.register_channel(channel_type, channel)
        service.start_channel(channel_type)

    socket_path = _daemon_socket_path(args)
    pid_path = _daemon_pid_path(args)

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    pid_path.write_text(f"{os.getpid()}\n")

    app = create_daemon_app(service)

    try:
        uvicorn.run(
            app,
            uds=str(socket_path),
            access_log=False,
            log_level="debug" if args.verbose else "info",
        )
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown()
        pid_path.unlink(missing_ok=True)
        socket_path.unlink(missing_ok=True)

    return 0


def cmd_daemon_start(args: argparse.Namespace) -> int:
    """Start the daemon as a detached background process."""
    import subprocess
    import time

    pid_path = _daemon_pid_path(args)
    socket_path = _daemon_socket_path(args)
    log_path = _daemon_log_path(args)
    wait_seconds = max(1.0, getattr(args, "wait_seconds", 8.0))

    pid = _read_pid(pid_path)
    if pid and _pid_is_running(pid):
        print(f"Daemon already running (pid {pid}).")
        return 0

    _cleanup_stale_daemon_files(pid_path, socket_path)

    repo_root = Path(__file__).resolve().parents[2]
    src_path = repo_root / "src"
    cmd = [
        sys.executable,
        "-m",
        "taskrunner",
        "--tasks-dir",
        str(args.tasks_dir),
        "--agent-config",
        str(args.agent_config),
    ]
    if args.containers:
        cmd.append("--containers")
    if args.no_judge:
        cmd.append("--no-judge")
    if args.verbose:
        cmd.append("--verbose")
    if args.json_logs:
        cmd.append("--json-logs")

    cmd.extend(
        [
            "daemon",
            "run",
            "--socket-path",
            str(socket_path),
            "--pid-file",
            str(pid_path),
            "--channel",
            args.channel_type,
        ]
    )
    if args.no_scheduler:
        cmd.append("--no-scheduler")

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    src_str = str(src_path)
    if existing_pythonpath:
        paths = existing_pythonpath.split(os.pathsep)
        if src_str not in paths:
            env["PYTHONPATH"] = src_str + os.pathsep + existing_pythonpath
    else:
        env["PYTHONPATH"] = src_str

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"Daemon failed to start. See log: {log_path}", file=sys.stderr)
            return 1
        if socket_path.exists():
            try:
                resp = _daemon_request(socket_path, "GET", "/health", timeout=0.5)
                if resp.status_code == 200:
                    daemon_pid = _read_pid(pid_path) or proc.pid
                    print(f"Daemon started (pid {daemon_pid}).")
                    print(f"Socket: {socket_path}")
                    print(f"Log: {log_path}")
                    return 0
            except Exception:
                pass
        time.sleep(0.1)

    print(f"Daemon did not become healthy within {wait_seconds:.1f}s. See {log_path}.", file=sys.stderr)
    return 1


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    """Stop the background daemon process."""
    import signal
    import time

    pid_path = _daemon_pid_path(args)
    socket_path = _daemon_socket_path(args)
    timeout = max(1.0, getattr(args, "timeout", 10.0))

    pid = _read_pid(pid_path)
    if not pid:
        print("Daemon is not running.")
        _cleanup_stale_daemon_files(pid_path, socket_path)
        return 0

    if not _pid_is_running(pid):
        print("Daemon pid file found, but process is not running. Cleaning up stale files.")
        _cleanup_stale_daemon_files(pid_path, socket_path)
        return 0

    os.kill(pid, signal.SIGTERM)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_is_running(pid):
            _cleanup_stale_daemon_files(pid_path, socket_path)
            print("Daemon stopped.")
            return 0
        time.sleep(0.1)

    print(f"Timed out waiting for daemon {pid} to stop.", file=sys.stderr)
    return 1


def cmd_daemon_status(args: argparse.Namespace) -> int:
    """Show daemon process and API health status."""
    pid_path = _daemon_pid_path(args)
    socket_path = _daemon_socket_path(args)

    pid = _read_pid(pid_path)
    if not pid or not _pid_is_running(pid):
        print("Daemon is not running.")
        if pid and not _pid_is_running(pid):
            _cleanup_stale_daemon_files(pid_path, socket_path)
        return 1

    print(f"Daemon running (pid {pid}).")
    print(f"Socket: {socket_path}")

    try:
        resp = _daemon_request(socket_path, "GET", "/v1/status", timeout=2.0)
        if resp.status_code != 200:
            print(f"API unhealthy (HTTP {resp.status_code}).", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"API unreachable: {e}", file=sys.stderr)
        return 1

    data = resp.json()
    print(f"Uptime: {data.get('uptime_seconds', 0)}s")
    sessions = data.get("sessions", {})
    print(
        "Sessions: "
        f"{sessions.get('stored', 0)} stored, {sessions.get('active_senders', 0)} active senders"
    )
    sched = data.get("scheduler", {})
    print(f"Scheduler: {'running' if sched.get('running') else 'stopped'}")

    channels = data.get("channels", [])
    if channels:
        print("Channels:")
        for ch in channels:
            state = "running" if ch.get("running") else "stopped"
            detail = ch.get("detail") or ""
            print(f"  - {ch.get('name')}: {state} ({detail})")
    else:
        print("Channels: none registered")

    return 0


def cmd_send(args: argparse.Namespace) -> int:
    """Send one message to the running daemon and print the response."""
    socket_path = _daemon_socket_path(args)
    payload = {
        "sender_id": args.sender_id,
        "text": args.message,
    }
    if args.session_id:
        payload["session_id"] = args.session_id

    try:
        resp = _daemon_request(
            socket_path,
            "POST",
            "/v1/messages",
            json_body=payload,
            timeout=max(1.0, args.timeout),
        )
    except Exception as e:
        print(f"Error: Could not reach daemon at {socket_path}: {e}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text
        print(f"Error: daemon request failed ({resp.status_code}) {detail}", file=sys.stderr)
        return 1

    data = resp.json()
    print(data.get("text", ""))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Query the guardian audit log."""
    from guardian.audit import read_audit_log
    from datetime import datetime

    try:
        agent_def = _load_agent_def(args)
    except FileNotFoundError:
        print(f"Error: Agent config not found at {args.agent_config}", file=sys.stderr)
        return 1

    log_file = agent_def.guardian.audit.log_file if agent_def.guardian else "guardian_audit.jsonl"
    tail = 0 if args.all else args.tail

    entries = read_audit_log(
        log_file,
        tail=tail,
        event_filter=args.event,
        blocked_only=args.blocked,
        denied_only=args.denied,
        tool_filter=getattr(args, "tool", None),
        since=getattr(args, "since", None),
    )

    if not entries:
        print("No audit entries found.")
        return 0

    # Format output
    for entry in entries:
        ts = entry.get("ts", "?")
        # Truncate to readable format
        if len(ts) > 19:
            ts = ts[:19]
        event = entry.get("event", "?")

        if event == "screen_input":
            status = "🚫 BLOCKED" if entry.get("blocked") else "✅ passed"
            source = entry.get("source", "?")
            conf = entry.get("confidence")
            conf_str = f" ({conf:.3f})" if conf is not None else ""
            print(f"[{ts}] {event}: {status} via {source}{conf_str}")

        elif event == "validate_action":
            verdict = entry.get("verdict", "?")
            icon = {"allow": "✅", "review": "⚠️", "deny": "🚫"}.get(verdict, "❓")
            tool = entry.get("tool_name", "?")
            rule = entry.get("matched_rule", "")
            print(f"[{ts}] {event}: {icon} {verdict} {tool} (rule: {rule or 'default'})")

        elif event == "tool_result":
            icon = "✅" if entry.get("success") else "❌"
            tool = entry.get("tool_name", "?")
            dur = entry.get("duration_ms", 0)
            size = entry.get("output_length", 0)
            err = entry.get("error", "")
            extra = f" error={err}" if err else ""
            print(f"[{ts}] {event}: {icon} {tool} {dur:.0f}ms ({size} chars){extra}")

        else:
            print(f"[{ts}] {event}: {entry}")

    print(f"\n{len(entries)} entries shown.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="creel",
        description="LLM Task Runner - secure, scheduled LLM task execution",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output"
    )
    parser.add_argument(
        "--containers", action="store_true", help="Run executors/LLM in Docker containers"
    )
    parser.add_argument(
        "--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR, help="Tasks directory"
    )
    parser.add_argument(
        "--agent-config", type=Path, default=DEFAULT_AGENT_CONFIG,
        help="Path to agent.yaml config",
    )
    parser.add_argument(
        "--json-logs", action="store_true",
        help="Output structured JSON log lines (for production)",
    )
    parser.add_argument(
        "--no-judge", action="store_true",
        help="Disable the LLM judge (Stage 2) to save API calls during development",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run command
    run_parser = subparsers.add_parser("run", help="Run a task immediately")
    run_parser.add_argument("task_name", help="Name of the task to run")
    run_parser.add_argument(
        "--dry", action="store_true", help="Dry run (render prompt only)"
    )

    # schedule command
    subparsers.add_parser("schedule", help="Start scheduler for all tasks")

    # list command
    subparsers.add_parser("list", help="List available tasks")

    # validate command
    validate_parser = subparsers.add_parser("validate", help="Validate a task definition")
    validate_parser.add_argument("task_name", help="Name of the task to validate")

    # attach command
    attach_parser = subparsers.add_parser("attach", help="Attach TUI to running daemon")
    attach_parser.add_argument(
        "--sender-id",
        default="cli",
        help="Sender ID/session namespace (default: cli)",
    )
    attach_parser.add_argument(
        "--new", action="store_true", help="Start and attach to a new session"
    )
    attach_parser.add_argument(
        "--resume", metavar="ID", help="Attach and resume a specific session"
    )
    attach_parser.add_argument(
        "--socket-path",
        type=Path,
        default=DEFAULT_DAEMON_SOCKET,
        help=f"Unix socket path (default: {DEFAULT_DAEMON_SOCKET})",
    )
    attach_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Request timeout in seconds (default: 300)",
    )


    # audit command
    audit_parser = subparsers.add_parser("audit", help="Query the guardian audit log")
    audit_parser.add_argument(
        "--tail", type=int, default=20, help="Show last N entries (default: 20)"
    )
    audit_parser.add_argument(
        "--blocked", action="store_true", help="Show only blocked input events"
    )
    audit_parser.add_argument(
        "--denied", action="store_true", help="Show only denied action events"
    )
    audit_parser.add_argument(
        "--event", type=str, default=None,
        help="Filter by event type (screen_input, validate_action, tool_result)"
    )
    audit_parser.add_argument(
        "--all", action="store_true", help="Show all entries (no tail limit)"
    )
    audit_parser.add_argument(
        "--tool", type=str, default=None,
        help="Filter by tool name"
    )
    audit_parser.add_argument(
        "--since", type=str, default=None,
        help="Show entries since date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
    )

    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Manage background daemon")
    daemon_subparsers = daemon_parser.add_subparsers(
        dest="daemon_command",
        metavar="{start,stop,status}",
    )

    daemon_start = daemon_subparsers.add_parser("start", help="Start the background daemon")
    daemon_start.add_argument(
        "--socket-path",
        type=Path,
        default=DEFAULT_DAEMON_SOCKET,
        help=f"Unix socket path (default: {DEFAULT_DAEMON_SOCKET})",
    )
    daemon_start.add_argument(
        "--pid-file",
        type=Path,
        default=DEFAULT_DAEMON_PID_FILE,
        help=f"PID file path (default: {DEFAULT_DAEMON_PID_FILE})",
    )
    daemon_start.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_DAEMON_LOG_FILE,
        help=f"Daemon log file (default: {DEFAULT_DAEMON_LOG_FILE})",
    )
    daemon_start.add_argument(
        "--channel",
        dest="channel_type",
        default="imessage",
        choices=["none", "imessage", "bluebubbles"],
        help="Channel plugin to run inside daemon (default: imessage)",
    )
    daemon_start.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Disable scheduler in daemon runtime",
    )
    daemon_start.add_argument(
        "--wait-seconds",
        type=float,
        default=8.0,
        help="Seconds to wait for daemon health check (default: 8)",
    )

    daemon_stop = daemon_subparsers.add_parser("stop", help="Stop the background daemon")
    daemon_stop.add_argument(
        "--socket-path",
        type=Path,
        default=DEFAULT_DAEMON_SOCKET,
        help=f"Unix socket path (default: {DEFAULT_DAEMON_SOCKET})",
    )
    daemon_stop.add_argument(
        "--pid-file",
        type=Path,
        default=DEFAULT_DAEMON_PID_FILE,
        help=f"PID file path (default: {DEFAULT_DAEMON_PID_FILE})",
    )
    daemon_stop.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Stop timeout in seconds (default: 10)",
    )

    daemon_status = daemon_subparsers.add_parser("status", help="Show daemon status")
    daemon_status.add_argument(
        "--socket-path",
        type=Path,
        default=DEFAULT_DAEMON_SOCKET,
        help=f"Unix socket path (default: {DEFAULT_DAEMON_SOCKET})",
    )
    daemon_status.add_argument(
        "--pid-file",
        type=Path,
        default=DEFAULT_DAEMON_PID_FILE,
        help=f"PID file path (default: {DEFAULT_DAEMON_PID_FILE})",
    )

    # Internal foreground command used by `daemon start`
    daemon_run = daemon_subparsers.add_parser("run", help=argparse.SUPPRESS)
    daemon_run.add_argument(
        "--socket-path",
        type=Path,
        default=DEFAULT_DAEMON_SOCKET,
    )
    daemon_run.add_argument(
        "--pid-file",
        type=Path,
        default=DEFAULT_DAEMON_PID_FILE,
    )
    daemon_run.add_argument(
        "--channel",
        dest="channel_type",
        default="imessage",
        choices=["none", "imessage", "bluebubbles"],
    )
    daemon_run.add_argument(
        "--no-scheduler",
        action="store_true",
    )
    daemon_subparsers._choices_actions = [  # type: ignore[attr-defined]
        action
        for action in daemon_subparsers._choices_actions  # type: ignore[attr-defined]
        if action.dest != "run"
    ]

    # send command
    send_parser = subparsers.add_parser("send", help="Send one message to the running daemon")
    send_parser.add_argument("message", help="Message text")
    send_parser.add_argument(
        "--sender-id",
        default="cli",
        help="Sender ID/session namespace (default: cli)",
    )
    send_parser.add_argument(
        "--session-id",
        default=None,
        help="Resume and send into this specific session",
    )
    send_parser.add_argument(
        "--socket-path",
        type=Path,
        default=DEFAULT_DAEMON_SOCKET,
        help=f"Unix socket path (default: {DEFAULT_DAEMON_SOCKET})",
    )
    send_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Request timeout in seconds (default: 300)",
    )

    args = parser.parse_args()

    # Set up logging
    from taskrunner.log import setup_logging

    log_level = "DEBUG" if args.verbose else "INFO"
    setup_logging(json_mode=args.json_logs, level=log_level)

    # Load root .env if present (for PHONE, etc.)
    root_env = Path(".env")
    if root_env.exists():
        for key, value in parse_env_file(root_env).items():
            os.environ.setdefault(key, value)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "daemon":
        daemon_commands = {
            "start": cmd_daemon_start,
            "stop": cmd_daemon_stop,
            "status": cmd_daemon_status,
            "run": cmd_daemon_run,
        }
        if args.daemon_command not in daemon_commands:
            daemon_parser.print_help()
            return 1
        return daemon_commands[args.daemon_command](args)

    commands = {
        "run": cmd_run,
        "schedule": cmd_schedule,
        "list": cmd_list,
        "validate": cmd_validate,
        "attach": cmd_attach,
        "audit": cmd_audit,
        "send": cmd_send,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
