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
    creel daemon install             Install daemon as launchd service
    creel daemon uninstall           Remove daemon launchd service
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
_CREEL_STATE_DIR = Path.home() / ".creel"
DEFAULT_DAEMON_SOCKET = _CREEL_STATE_DIR / "daemon.sock"
DEFAULT_DAEMON_PID_FILE = _CREEL_STATE_DIR / "daemon.pid"
DEFAULT_DAEMON_LOG_FILE = _CREEL_STATE_DIR / "daemon.log"
DEFAULT_DAEMON_LABEL = "com.creel.daemon"
DEFAULT_DAEMON_PLIST_FILE = (
    Path.home() / "Library" / "LaunchAgents" / f"{DEFAULT_DAEMON_LABEL}.plist"
)


def _tasks_dir(args: argparse.Namespace) -> Path:
    return args.tasks_dir


def _daemon_socket_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "socket_path", DEFAULT_DAEMON_SOCKET))


def _daemon_pid_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "pid_file", DEFAULT_DAEMON_PID_FILE))


def _daemon_log_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "log_file", DEFAULT_DAEMON_LOG_FILE))


def _daemon_label(args: argparse.Namespace) -> str:
    return str(getattr(args, "label", DEFAULT_DAEMON_LABEL))


def _daemon_plist_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "plist_path", DEFAULT_DAEMON_PLIST_FILE))


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


def _daemon_launchd_target() -> str:
    return f"gui/{os.getuid()}"


def _allow_launchd_bootout_failure(output: str) -> bool:
    lower = output.lower()
    return any(
        phrase in lower
        for phrase in (
            "could not find service",
            "service not loaded",
            "not found",
            "no such process",
        )
    )


def _allow_launchd_bootstrap_failure(output: str) -> bool:
    return "already loaded" in output.lower()


def _build_daemon_run_command(args: argparse.Namespace, socket_path: Path, pid_path: Path) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "taskrunner",
        "--tasks-dir",
        str(Path(args.tasks_dir).resolve()),
        "--agent-config",
        str(Path(args.agent_config).resolve()),
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
    return cmd


def _build_daemon_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    src_path = repo_root / "src"
    existing_pythonpath = env.get("PYTHONPATH", "")
    src_str = str(src_path)
    if existing_pythonpath:
        paths = existing_pythonpath.split(os.pathsep)
        if src_str not in paths:
            env["PYTHONPATH"] = src_str + os.pathsep + existing_pythonpath
    else:
        env["PYTHONPATH"] = src_str
    return env


def _wait_for_daemon_health(socket_path: Path, wait_seconds: float) -> bool:
    import time

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if socket_path.exists():
            try:
                resp = _daemon_request(socket_path, "GET", "/health", timeout=0.5)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
        time.sleep(0.1)
    return False


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

    # Fetch daemon status for startup banner
    tool_count = 0
    guardian_active = False
    try:
        daemon_status = client.status()
        tool_count = daemon_status.get("tool_count", 0)
        guardian_active = daemon_status.get("guardian_active", False)
    except Exception:
        pass  # non-critical, banner will show defaults

    app = ChatApp(
        backend,
        sender_id=sender_id,
        tool_count=tool_count,
        guardian_active=guardian_active,
    )
    app.run()
    return 0


def _build_daemon_channel(agent_def, channel_type: str):
    """Build an optional channel plugin for daemon runtime.

    Uses the plugin registry to discover and instantiate channels.
    Falls back to direct construction for backward compatibility.
    """
    if channel_type == "none":
        return None, None

    from taskrunner.channels.registry import ChannelRegistry
    from taskrunner.channels.plugin import ChannelCapability

    registry = ChannelRegistry()
    registry.discover()

    # Get config from agent_def
    config = agent_def.channels.get_channel_config(channel_type)
    if config is None:
        raise ValueError(f"No {channel_type} channel configured in agent.yaml")

    entry = registry.get(channel_type)
    if entry is not None:
        channel = registry.create_channel(channel_type, config)
        # Return channel as reply_channel if it supports WAIT_FOR_REPLY
        reply_channel = (
            channel
            if ChannelCapability.WAIT_FOR_REPLY in entry.meta.capabilities
            else None
        )
        return channel, reply_channel

    raise ValueError(
        f"Unknown channel type '{channel_type}'. "
        f"Available: {', '.join(m.id for m in registry.available())}"
    )


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
        channel, reply_channel = _build_daemon_channel(agent_def, channel_type)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    server = ChatServer(
        agent_def,
        use_containers=args.containers,
        reply_channel=reply_channel,
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

    guardian_status = "active" if server._guardian else "inactive"
    tool_count = len(agent_def.tools)
    print(f"🧺 Creel agent ready. Tools loaded: {tool_count}. Guardian: {guardian_status}.")

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

    pid_path = _daemon_pid_path(args)
    socket_path = _daemon_socket_path(args)
    log_path = _daemon_log_path(args)
    plist_path = _daemon_plist_path(args)
    label = _daemon_label(args)
    wait_seconds = max(1.0, getattr(args, "wait_seconds", 8.0))

    # If a launchd service is installed, use it as the startup path.
    if sys.platform == "darwin" and plist_path.exists():
        launch_target = _daemon_launchd_target()
        existing = subprocess.run(
            ["launchctl", "bootstrap", launch_target, str(plist_path)],
            capture_output=True,
            text=True,
        )
        existing_out = (existing.stdout or "") + "\n" + (existing.stderr or "")
        if existing.returncode != 0 and not _allow_launchd_bootstrap_failure(existing_out):
            print(
                f"Error: failed to bootstrap launchd service {label}: {existing_out.strip()}",
                file=sys.stderr,
            )
            return 1

        kickstart = subprocess.run(
            ["launchctl", "kickstart", "-k", f"{launch_target}/{label}"],
            capture_output=True,
            text=True,
        )
        if kickstart.returncode != 0:
            output = (kickstart.stdout or "") + "\n" + (kickstart.stderr or "")
            print(
                f"Error: failed to start launchd service {label}: {output.strip()}",
                file=sys.stderr,
            )
            return 1

        if _wait_for_daemon_health(socket_path, wait_seconds):
            daemon_pid = _read_pid(pid_path)
            if daemon_pid:
                print(f"Daemon started via launchd (pid {daemon_pid}).")
            else:
                print(f"Daemon started via launchd ({label}).")
            print(f"Socket: {socket_path}")
            print(f"Log: {log_path}")
            return 0

        print(
            f"Launchd service {label} did not become healthy within {wait_seconds:.1f}s. See {log_path}.",
            file=sys.stderr,
        )
        return 1

    pid = _read_pid(pid_path)
    if pid and _pid_is_running(pid):
        print(f"Daemon already running (pid {pid}).")
        return 0

    _cleanup_stale_daemon_files(pid_path, socket_path)

    repo_root = Path(__file__).resolve().parents[2]
    cmd = _build_daemon_run_command(args, socket_path, pid_path)
    env = _build_daemon_env(repo_root)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    import time

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"Daemon failed to start. See log: {log_path}", file=sys.stderr)
            return 1
        if _wait_for_daemon_health(socket_path, 0.2):
            daemon_pid = _read_pid(pid_path) or proc.pid
            print(f"Daemon started (pid {daemon_pid}).")
            print(f"Socket: {socket_path}")
            print(f"Log: {log_path}")
            return 0

    print(f"Daemon did not become healthy within {wait_seconds:.1f}s. See {log_path}.", file=sys.stderr)
    return 1


def cmd_daemon_install(args: argparse.Namespace) -> int:
    """Install daemon into launchd and start it."""
    import plistlib
    import subprocess

    if sys.platform != "darwin":
        print("Error: launchd install is only supported on macOS.", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parents[2]
    socket_path = _daemon_socket_path(args)
    pid_path = _daemon_pid_path(args)
    log_path = _daemon_log_path(args)
    plist_path = _daemon_plist_path(args)
    label = _daemon_label(args)
    wait_seconds = max(1.0, getattr(args, "wait_seconds", 8.0))
    launch_target = _daemon_launchd_target()

    cmd = _build_daemon_run_command(args, socket_path, pid_path)
    env = _build_daemon_env(repo_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    plist_payload = {
        "Label": label,
        "ProgramArguments": cmd,
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {"PYTHONPATH": env.get("PYTHONPATH", "")},
    }
    with plist_path.open("wb") as f:
        plistlib.dump(plist_payload, f)

    # Remove a previously loaded service instance so bootstrap is deterministic.
    bootout = subprocess.run(
        ["launchctl", "bootout", f"{launch_target}/{label}"],
        capture_output=True,
        text=True,
    )
    bootout_out = (bootout.stdout or "") + "\n" + (bootout.stderr or "")
    if bootout.returncode != 0 and not _allow_launchd_bootout_failure(bootout_out):
        print(
            f"Error: failed to unload existing launchd service {label}: {bootout_out.strip()}",
            file=sys.stderr,
        )
        return 1

    bootstrap = subprocess.run(
        ["launchctl", "bootstrap", launch_target, str(plist_path)],
        capture_output=True,
        text=True,
    )
    bootstrap_out = (bootstrap.stdout or "") + "\n" + (bootstrap.stderr or "")
    if bootstrap.returncode != 0 and not _allow_launchd_bootstrap_failure(bootstrap_out):
        print(
            f"Error: failed to bootstrap launchd service {label}: {bootstrap_out.strip()}",
            file=sys.stderr,
        )
        return 1

    kickstart = subprocess.run(
        ["launchctl", "kickstart", "-k", f"{launch_target}/{label}"],
        capture_output=True,
        text=True,
    )
    if kickstart.returncode != 0:
        out = (kickstart.stdout or "") + "\n" + (kickstart.stderr or "")
        print(f"Error: failed to start launchd service {label}: {out.strip()}", file=sys.stderr)
        return 1

    if not _wait_for_daemon_health(socket_path, wait_seconds):
        print(
            f"Launchd service {label} installed but daemon did not become healthy "
            f"within {wait_seconds:.1f}s. See {log_path}.",
            file=sys.stderr,
        )
        return 1

    pid = _read_pid(pid_path)
    if pid:
        print(f"Daemon launchd service installed and running (pid {pid}).")
    else:
        print("Daemon launchd service installed and running.")
    print(f"Label: {label}")
    print(f"Plist: {plist_path}")
    print(f"Socket: {socket_path}")
    print(f"Log: {log_path}")
    return 0


def cmd_daemon_uninstall(args: argparse.Namespace) -> int:
    """Unload daemon from launchd and remove plist."""
    import subprocess

    if sys.platform != "darwin":
        print("Error: launchd uninstall is only supported on macOS.", file=sys.stderr)
        return 1

    pid_path = _daemon_pid_path(args)
    socket_path = _daemon_socket_path(args)
    plist_path = _daemon_plist_path(args)
    label = _daemon_label(args)
    launch_target = _daemon_launchd_target()

    bootout = subprocess.run(
        ["launchctl", "bootout", f"{launch_target}/{label}"],
        capture_output=True,
        text=True,
    )
    bootout_out = (bootout.stdout or "") + "\n" + (bootout.stderr or "")
    if bootout.returncode != 0 and not _allow_launchd_bootout_failure(bootout_out):
        print(
            f"Error: failed to unload launchd service {label}: {bootout_out.strip()}",
            file=sys.stderr,
        )
        return 1

    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed plist: {plist_path}")
    else:
        print(f"Plist not found (already removed): {plist_path}")

    _cleanup_stale_daemon_files(pid_path, socket_path)
    print(f"Daemon launchd service '{label}' uninstalled.")
    return 0


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    """Stop the background daemon process."""
    import signal
    import subprocess
    import time

    pid_path = _daemon_pid_path(args)
    socket_path = _daemon_socket_path(args)
    plist_path = _daemon_plist_path(args)
    label = _daemon_label(args)
    timeout = max(1.0, getattr(args, "timeout", 10.0))

    if sys.platform == "darwin" and plist_path.exists():
        launch_target = _daemon_launchd_target()
        bootout = subprocess.run(
            ["launchctl", "bootout", f"{launch_target}/{label}"],
            capture_output=True,
            text=True,
        )
        bootout_out = (bootout.stdout or "") + "\n" + (bootout.stderr or "")
        if bootout.returncode != 0 and not _allow_launchd_bootout_failure(bootout_out):
            print(
                f"Error: failed to stop launchd service {label}: {bootout_out.strip()}",
                file=sys.stderr,
            )
            return 1

        _cleanup_stale_daemon_files(pid_path, socket_path)
        print(f"Daemon stopped (launchd service '{label}' unloaded).")
        return 0

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

    print(
        f"Timed out waiting for daemon {pid} to stop. "
        f"You can force-kill it with: kill -9 {pid}",
        file=sys.stderr,
    )
    return 1


def cmd_daemon_status(args: argparse.Namespace) -> int:
    """Show daemon process and API health status."""
    import subprocess

    pid_path = _daemon_pid_path(args)
    socket_path = _daemon_socket_path(args)
    plist_path = _daemon_plist_path(args)
    label = _daemon_label(args)

    launchd_loaded = False
    if sys.platform == "darwin" and plist_path.exists():
        launch_target = _daemon_launchd_target()
        launchd = subprocess.run(
            ["launchctl", "print", f"{launch_target}/{label}"],
            capture_output=True,
            text=True,
        )
        launchd_loaded = launchd.returncode == 0

    pid = _read_pid(pid_path)
    if not pid or not _pid_is_running(pid):
        if _wait_for_daemon_health(socket_path, 0.2):
            print("Daemon running (pid unknown).")
            print(f"Socket: {socket_path}")
        else:
            print("Daemon is not running.")
            if pid and not _pid_is_running(pid):
                _cleanup_stale_daemon_files(pid_path, socket_path)
            if sys.platform == "darwin" and plist_path.exists():
                state = "loaded" if launchd_loaded else "not loaded"
                print(f"Launchd: {state} ({label})")
                print(f"Plist: {plist_path}")
            return 1

    else:
        print(f"Daemon running (pid {pid}).")
        print(f"Socket: {socket_path}")

    if sys.platform == "darwin" and plist_path.exists():
        state = "loaded" if launchd_loaded else "not loaded"
        print(f"Launchd: {state} ({label})")
        print(f"Plist: {plist_path}")

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

    if getattr(args, "stream", False):
        from taskrunner.daemon.client import DaemonApiClient

        client = DaemonApiClient(
            socket_path=socket_path,
            timeout=max(1.0, args.timeout),
        )
        try:
            saw_token = False
            for event in client.stream_message(
                sender_id=args.sender_id,
                text=args.message,
                session_id=args.session_id,
            ):
                event_type = str(event.get("type", ""))
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    payload = {}

                if event_type == "token":
                    chunk = str(payload.get("text", ""))
                    if chunk:
                        saw_token = True
                        print(chunk, end="", flush=True)
                elif event_type == "final":
                    final_text = str(payload.get("text", ""))
                    if final_text and not saw_token:
                        print(final_text, end="", flush=True)
                elif event_type == "error":
                    err = payload.get("error", "streaming request failed")
                    print(f"\nError: {err}", file=sys.stderr)
                    return 1
            print()
            return 0
        except Exception as e:
            print(f"Error: Could not stream from daemon at {socket_path}: {e}", file=sys.stderr)
            return 1

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

    # --- Shared daemon parent parsers (to avoid option duplication) ---
    _daemon_paths_parent = argparse.ArgumentParser(add_help=False)
    _daemon_paths_parent.add_argument(
        "--socket-path", type=Path, default=DEFAULT_DAEMON_SOCKET,
        help=f"Unix socket path (default: {DEFAULT_DAEMON_SOCKET})",
    )
    _daemon_paths_parent.add_argument(
        "--pid-file", type=Path, default=DEFAULT_DAEMON_PID_FILE,
        help=f"PID file path (default: {DEFAULT_DAEMON_PID_FILE})",
    )

    _daemon_launchd_parent = argparse.ArgumentParser(add_help=False)
    _daemon_launchd_parent.add_argument(
        "--label", default=DEFAULT_DAEMON_LABEL,
        help=f"launchd service label (default: {DEFAULT_DAEMON_LABEL})",
    )
    _daemon_launchd_parent.add_argument(
        "--plist-path", type=Path, default=DEFAULT_DAEMON_PLIST_FILE,
        help=f"launchd plist path (default: {DEFAULT_DAEMON_PLIST_FILE})",
    )

    _daemon_runtime_parent = argparse.ArgumentParser(add_help=False)
    _daemon_runtime_parent.add_argument(
        "--log-file", type=Path, default=DEFAULT_DAEMON_LOG_FILE,
        help=f"Daemon log file (default: {DEFAULT_DAEMON_LOG_FILE})",
    )
    _daemon_runtime_parent.add_argument(
        "--channel", dest="channel_type", default="imessage",
        help="Channel plugin to run inside daemon (default: imessage). Use 'none' to disable.",
    )
    _daemon_runtime_parent.add_argument(
        "--no-scheduler", action="store_true",
        help="Disable scheduler in daemon runtime",
    )
    _daemon_runtime_parent.add_argument(
        "--wait-seconds", type=float, default=8.0,
        help="Seconds to wait for daemon health check (default: 8)",
    )

    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Manage background daemon")
    daemon_subparsers = daemon_parser.add_subparsers(
        dest="daemon_command",
        metavar="{start,stop,status,install,uninstall}",
    )

    daemon_subparsers.add_parser(
        "start", help="Start the background daemon",
        parents=[_daemon_paths_parent, _daemon_launchd_parent, _daemon_runtime_parent],
    )

    daemon_stop = daemon_subparsers.add_parser(
        "stop", help="Stop the background daemon",
        parents=[_daemon_paths_parent, _daemon_launchd_parent],
    )
    daemon_stop.add_argument(
        "--timeout", type=float, default=10.0,
        help="Stop timeout in seconds (default: 10)",
    )

    daemon_subparsers.add_parser(
        "status", help="Show daemon status",
        parents=[_daemon_paths_parent, _daemon_launchd_parent],
    )

    daemon_subparsers.add_parser(
        "install", help="Install daemon as a launchd service",
        parents=[_daemon_paths_parent, _daemon_launchd_parent, _daemon_runtime_parent],
    )

    daemon_subparsers.add_parser(
        "uninstall", help="Uninstall daemon launchd service",
        parents=[_daemon_paths_parent, _daemon_launchd_parent],
    )

    # Internal foreground command used by `daemon start`
    daemon_run = daemon_subparsers.add_parser(
        "run", help=argparse.SUPPRESS,
        parents=[_daemon_paths_parent],
    )
    daemon_run.add_argument(
        "--channel", dest="channel_type", default="imessage",
    )
    daemon_run.add_argument("--no-scheduler", action="store_true")
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
    send_parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream response events from daemon (SSE)",
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
            "install": cmd_daemon_install,
            "uninstall": cmd_daemon_uninstall,
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
