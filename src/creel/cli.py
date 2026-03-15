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
    creel doctor                     Check installation health
    creel doctor --fix               Auto-fix remediable issues
"""

from __future__ import annotations

# Use OS certificate store for HTTPS (fixes uv-managed Python SSL issues)
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import asyncio
import logging
import os
import socket as _socket
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from creel import paths

if TYPE_CHECKING:
    from creel.knowledge_base import KnowledgeBase
from creel.models import load_all_tasks, load_task
from creel.orchestrator import run_task
from creel.scheduler import start_scheduler
from creel.secrets import parse_env_file

logger = logging.getLogger(__name__)

DEFAULT_DAEMON_LABEL = "com.creel.daemon"


def _default_tasks_dir() -> Path:
    return paths.tasks_dir()


def _default_agent_config() -> Path:
    return paths.agent_config()


def _default_daemon_socket() -> Path:
    return paths.creel_home() / "daemon.sock"


def _default_daemon_pid_file() -> Path:
    return paths.creel_home() / "daemon.pid"


def _default_daemon_log_file() -> Path:
    return paths.creel_home() / "daemon.log"


def _default_daemon_plist_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{DEFAULT_DAEMON_LABEL}.plist"


def _tasks_dir(args: argparse.Namespace) -> Path:
    return args.tasks_dir or _default_tasks_dir()


def _daemon_socket_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "socket_path", None) or _default_daemon_socket())


def _daemon_pid_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "pid_file", None) or _default_daemon_pid_file())


def _daemon_log_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "log_file", None) or _default_daemon_log_file())


def _daemon_label(args: argparse.Namespace) -> str:
    return str(getattr(args, "label", DEFAULT_DAEMON_LABEL))


def _daemon_plist_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "plist_path", None) or _default_daemon_plist_file())


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


def _build_daemon_run_command(
    args: argparse.Namespace, socket_path: Path, pid_path: Path
) -> list[str]:
    exe = paths.creel_executable()
    if exe:
        cmd = [exe]
    else:
        cmd = [sys.executable, "-m", "creel"]
    cmd.extend(
        [
            "--tasks-dir",
            str(Path(args.tasks_dir).resolve()),
            "--agent-config",
            str(Path(args.agent_config or _default_agent_config()).resolve()),
        ]
    )
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
    if getattr(args, "http_port", None) is not None:
        cmd.extend(["--http-port", str(args.http_port)])
    return cmd


def _print_log_errors(log_path: Path, offset: int = 0) -> None:
    """Print recent error lines from the daemon log (only from bytes after *offset*)."""
    try:
        with log_path.open(encoding="utf-8") as f:
            f.seek(offset)
            new_text = f.read()
        lines = new_text.splitlines()
        error_lines = [line for line in lines if line.startswith("Error:")]
        if error_lines:
            for line in error_lines[-3:]:
                print(f"  {line}", file=sys.stderr)
        elif lines:
            for line in lines[-3:]:
                print(f"  {line}", file=sys.stderr)
    except OSError:
        pass


def _wait_for_daemon_health(socket_path: Path, wait_seconds: float) -> bool:
    """Wait until the daemon health endpoint responds.

    Accepts ``"starting"``, ``"ok"``, and ``"error"`` statuses — any 200
    response means the socket is bound and the process is alive.  An
    ``"error"`` status means initialization failed but the process is
    reachable, so we still return ``True`` (the caller can inspect the
    health body for details).
    """
    import time

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if socket_path.exists():
            try:
                resp = _daemon_request(socket_path, "GET", "/health", timeout=0.5)
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("status") in ("ok", "starting", "error"):
                        return True
            except Exception:
                pass
        time.sleep(0.1)
    return False


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold ~/.creel/ directory structure."""
    from creel.init import init, migrate

    if args.migrate:
        repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
        if not (repo_root / "agent.yaml").exists() and not args.repo_root:
            print(
                "No agent.yaml found in current directory. "
                "Use --repo-root to specify the repo location.",
                file=sys.stderr,
            )
            return 1
        lines = migrate(repo_root, force=args.force)
    else:
        parsed_tools = (
            [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else None
        )
        lines = init(
            force=args.force,
            interactive=not args.non_interactive,
            provider=args.provider,
            api_key=args.api_key,
            model=args.model,
            channel=args.channel,
            bot_token=args.bot_token,
            allowed_senders=args.allowed_senders,
            tools=parsed_tools,
            enable_media=args.enable_media,
            enable_guardian=not args.no_guardian,
        )

    print(f"Creel home: {paths.creel_home()}")
    for line in lines:
        print(line)

    return 0


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
        print(
            f"{task.name:<25} {task.schedule:<20} {executors_list:<30} {task.output.type}:{task.output.to}"
        )

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
    from creel.models import load_agent_config

    config_path = args.agent_config or _default_agent_config()
    agent_def = load_agent_config(config_path)

    # CLI overrides
    if getattr(args, "no_judge", False) and agent_def.guardian:
        agent_def.guardian.llm_judge.enabled = False
        logger.info("LLM judge disabled via --no-judge flag")

    return agent_def


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach Textual TUI to a running daemon."""
    from creel.daemon.client import DaemonApiClient, DaemonTuiAdapter
    from creel.tui import ChatApp

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

    from creel.channels.plugin import ChannelCapability
    from creel.channels.registry import ChannelRegistry

    registry = ChannelRegistry()
    registry.discover()

    # Get config from agent_def
    config = agent_def.channels.get_channel_config(channel_type)
    if config is None:
        raise ValueError(f"No {channel_type} channel configured in agent.yaml")

    entry = registry.get(channel_type)
    if entry is not None:
        channel = registry.create_channel(channel_type, config)
        # Return channel as reply_channel if it can send messages
        reply_channel = channel if ChannelCapability.SEND in entry.meta.capabilities else None
        return channel, reply_channel

    raise ValueError(
        f"Unknown channel type '{channel_type}'. "
        f"Available: {', '.join(m.id for m in registry.available())}"
    )


def _start_bridge_server(bridge_config) -> threading.Thread:
    """Start the host bridge server in a background thread.

    Pre-generates scoped tokens and sets them as env vars so both the
    bridge server and the orchestrator (for container injection) can
    use them.
    """
    import secrets as _secrets

    # Parse host/port from bridge URL
    from urllib.parse import urlparse

    import uvicorn as _uvicorn

    parsed = urlparse(bridge_config.url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8099

    # Pre-generate scoped tokens so orchestrator can inject them into containers
    from creel.orchestrator import _EXECUTOR_TO_BRIDGE_SCOPE

    scopes = sorted(set(_EXECUTOR_TO_BRIDGE_SCOPE.values()))
    for scope in scopes:
        env_var = f"BRIDGE_TOKEN_{scope}"
        if not os.environ.get(env_var):
            os.environ[env_var] = _secrets.token_urlsafe(32)

    def _run_bridge():
        try:
            from bridge.server import app as bridge_app

            logger.info("Starting bridge server on %s:%d", host, port)
            _uvicorn.run(
                bridge_app,
                host=host,
                port=port,
                access_log=False,
                log_level="info",
            )
        except Exception:
            logger.exception("Bridge server failed")

    t = threading.Thread(target=_run_bridge, daemon=True, name="bridge-server")
    t.start()
    return t


def cmd_daemon_run(args: argparse.Namespace) -> int:
    """Run the daemon server in the foreground (internal command).

    Starts uvicorn **first** so the Unix socket is available immediately for
    health checks, then performs heavy initialization (ChatServer, Guardian,
    channels, scheduler, etc.) in a background thread via the FastAPI lifespan.
    """
    import uvicorn

    from creel.daemon.api import create_daemon_app
    from creel.startup import SecretsValidationError, validate_secrets

    # --- Fast, required pre-checks (before socket bind) ---
    try:
        agent_def = _load_agent_def(args)
    except FileNotFoundError:
        print(
            f"Error: Agent config not found at {args.agent_config or _default_agent_config()}",
            file=sys.stderr,
        )
        return 1

    try:
        validate_secrets(agent_def)
    except SecretsValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if agent_def.llm.secrets:
        from creel.orchestrator import _load_secrets_to_env

        _load_secrets_to_env(agent_def.llm.secrets)

    socket_path = _daemon_socket_path(args)
    pid_path = _daemon_pid_path(args)

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    pid_path.write_text(f"{os.getpid()}\n")

    # --- Deferred heavy init (runs after socket is bound) ---
    def _deferred_init():
        from creel.chat import ChatServer
        from creel.daemon.service import DaemonService

        channel_type = getattr(args, "channel_type", "imessage")
        try:
            channel, reply_channel = _build_daemon_channel(agent_def, channel_type)
        except ValueError as e:
            print(f"Error building {channel_type} channel: {e}", file=sys.stderr)
            raise RuntimeError(f"Channel configuration error: {e}") from e

        config_path = args.agent_config or _default_agent_config()

        server = ChatServer(
            agent_def,
            use_containers=args.containers,
            reply_channel=reply_channel,
        )
        service = DaemonService(
            agent_def,
            use_containers=args.containers,
            server=server,
            config_path=config_path,
        )

        if not getattr(args, "no_scheduler", False):
            tasks_dir = _tasks_dir(args)
            if tasks_dir.is_dir():
                service.start_scheduler(tasks_dir)

        if channel is not None:
            service.register_channel(channel_type, channel)
            service.start_channel(channel_type)

        guardian_status = "active" if server._guardian else "inactive"
        tool_count = len(agent_def.tools)
        logger.info(
            "Creel agent ready. Tools loaded: %d. Guardian: %s.", tool_count, guardian_status
        )

        # Pre-build Docker images in background so they're ready before first use
        if args.containers:
            from creel.orchestrator import prebuild_images

            prebuild_images(agent_def)  # fire-and-forget; daemon threads

        # Start host bridge server if enabled
        if getattr(agent_def, "bridge", None) and agent_def.bridge.enabled:
            _start_bridge_server(agent_def.bridge)

        # Install SIGHUP handler for config reload.
        # Signal handlers run on the main thread and can interrupt code holding
        # locks, so we defer the actual reload to a background thread.
        import signal as _signal

        def _sighup_handler(signum, frame):
            logger.info("Received SIGHUP — scheduling config reload")
            t = threading.Thread(
                target=_deferred_sighup_reload,
                name="creel-sighup-reload",
                daemon=True,
            )
            t.start()

        def _deferred_sighup_reload():
            try:
                result = service.reload_config()
                logger.info("SIGHUP reload: %s", result.summary())
            except Exception:
                logger.exception("SIGHUP config reload failed")

        _signal.signal(_signal.SIGHUP, _sighup_handler)

        # Start file watcher for automatic config reload
        from creel.daemon.watcher import ConfigWatcher

        watcher = ConfigWatcher(
            config_path=Path(config_path),
            on_change=lambda: service.reload_config(),
        )
        watcher.start()
        service._config_watcher = watcher

        return service

    app = create_daemon_app(init_factory=_deferred_init)

    http_port = getattr(args, "http_port", None) or None

    # Build sockets: always a unix socket for CLI clients, optionally a TCP
    # socket for browser access.  Passing both to a single uvicorn Server
    # means one lifespan, one event loop, and WebSocket support on both.
    _sockets: list[_socket.socket] = []

    _unix_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    _unix_sock.bind(str(socket_path))
    _unix_sock.listen(128)
    _sockets.append(_unix_sock)

    if http_port:
        _tcp_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _tcp_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _tcp_sock.bind(("127.0.0.1", http_port))
        _tcp_sock.listen(128)
        _sockets.append(_tcp_sock)
        print(f"Dashboard available at http://localhost:{http_port}")

    _config = uvicorn.Config(
        app,
        access_log=False,
        log_level="debug" if args.verbose else "info",
    )
    _server = uvicorn.Server(_config)

    try:
        asyncio.run(_server.serve(sockets=_sockets))
    except KeyboardInterrupt:
        pass
    finally:
        for _sock in _sockets:
            try:
                _sock.close()
            except Exception:
                pass
        # Lifespan teardown calls service.shutdown() if a service was created.
        # We only need to clean up PID/socket files here.
        print("Daemon stopped.")
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
    wait_seconds = max(1.0, getattr(args, "wait_seconds", 20.0))

    # If a launchd service is installed, use it as the startup path.
    if sys.platform == "darwin" and plist_path.exists():
        launchd_log_offset = log_path.stat().st_size if log_path.exists() else 0
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
        _print_log_errors(log_path, launchd_log_offset)
        return 1

    pid = _read_pid(pid_path)
    if pid and _pid_is_running(pid):
        print(f"Daemon already running (pid {pid}).")
        return 0

    _cleanup_stale_daemon_files(pid_path, socket_path)

    cmd = _build_daemon_run_command(args, socket_path, pid_path)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    with log_path.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    import time

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"Daemon failed to start. See log: {log_path}", file=sys.stderr)
            _print_log_errors(log_path, log_offset)
            return 1
        if _wait_for_daemon_health(socket_path, 0.2):
            daemon_pid = _read_pid(pid_path) or proc.pid
            print(f"Daemon started (pid {daemon_pid}).")
            print(f"Socket: {socket_path}")
            print(f"Log: {log_path}")
            return 0

    print(
        f"Daemon did not become healthy within {wait_seconds:.1f}s. See {log_path}.",
        file=sys.stderr,
    )
    _print_log_errors(log_path, log_offset)
    return 1


def cmd_daemon_install(args: argparse.Namespace) -> int:
    """Install daemon into launchd and start it."""
    import plistlib
    import subprocess

    if sys.platform != "darwin":
        print("Error: launchd install is only supported on macOS.", file=sys.stderr)
        return 1

    socket_path = _daemon_socket_path(args)
    pid_path = _daemon_pid_path(args)
    log_path = _daemon_log_path(args)
    plist_path = _daemon_plist_path(args)
    label = _daemon_label(args)
    wait_seconds = max(1.0, getattr(args, "wait_seconds", 20.0))
    launch_target = _daemon_launchd_target()

    cmd = _build_daemon_run_command(args, socket_path, pid_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    plist_payload = {
        "Label": label,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
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
        print(
            f"Error: failed to start launchd service {label}: {out.strip()}",
            file=sys.stderr,
        )
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
        f"Timed out waiting for daemon {pid} to stop. You can force-kill it with: kill -9 {pid}",
        file=sys.stderr,
    )
    return 1


def cmd_daemon_restart(args: argparse.Namespace) -> int:
    """Stop then start the daemon."""
    import time

    cmd_daemon_stop(args)
    time.sleep(0.5)
    return cmd_daemon_start(args)


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

    # Check bridge server health
    try:
        agent_def = _load_agent_def(args)
        if getattr(agent_def, "bridge", None) and agent_def.bridge.enabled:
            import httpx

            bridge_url = agent_def.bridge.url.rstrip("/")
            try:
                br = httpx.get(f"{bridge_url}/health", timeout=1.0)
                if br.status_code == 200:
                    print(f"Bridge: running ({bridge_url})")
                else:
                    print(f"Bridge: unhealthy (HTTP {br.status_code})")
            except Exception:
                print(f"Bridge: not reachable ({bridge_url})")
    except Exception:
        pass  # No agent config or bridge not configured

    return 0


def cmd_reload(args: argparse.Namespace) -> int:
    """Hot-reload daemon config without restarting."""
    socket_path = _daemon_socket_path(args)

    try:
        resp = _daemon_request(socket_path, "POST", "/api/config/reload", timeout=10.0)
    except Exception as e:
        print(f"Error: Could not reach daemon at {socket_path}: {e}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text
        print(f"Reload failed: {detail}", file=sys.stderr)
        return 1

    data = resp.json()
    print(data.get("summary", "Config reloaded."))

    changes = data.get("changes", [])
    for change in changes:
        print(f"  - {change['field']}")

    non_reloadable = data.get("non_reloadable", [])
    if non_reloadable:
        print("\nNon-reloadable changes (restart required):")
        for nr in non_reloadable:
            print(f"  - {nr['field']}: {nr['message']}")

    return 0


def cmd_send(args: argparse.Namespace) -> int:
    """Send one message to the running daemon and print the response."""
    socket_path = _daemon_socket_path(args)

    if getattr(args, "stream", False):
        from creel.daemon.client import DaemonApiClient

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
                auto_approve=args.auto_approve,
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
            print(
                f"Error: Could not stream from daemon at {socket_path}: {e}",
                file=sys.stderr,
            )
            return 1

    payload = {
        "sender_id": args.sender_id,
        "text": args.message,
    }
    if args.session_id:
        payload["session_id"] = args.session_id
    if args.auto_approve:
        payload["auto_approve"] = True

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
        print(
            f"Error: daemon request failed ({resp.status_code}) {detail}",
            file=sys.stderr,
        )
        return 1

    data = resp.json()
    print(data.get("text", ""))
    return 0


def _cron_store(args: argparse.Namespace):
    """Create a JobStore from default or overridden paths."""
    from creel.cron.store import JobStore

    cron_dir = Path(getattr(args, "cron_dir", paths.cron_dir()))
    return JobStore(
        jobs_path=cron_dir / "jobs.json",
        runs_path=cron_dir / "runs.json",
    )


def _format_schedule(schedule) -> str:
    """Format a Schedule model for display."""
    if schedule.kind == "cron":
        return f"cron: {schedule.expr}"
    elif schedule.kind == "every":
        return f"every {schedule.expr}s"
    elif schedule.kind == "at":
        return f"at {schedule.expr}"
    return str(schedule.expr)


def cmd_cron_list(args: argparse.Namespace) -> int:
    """List all cron jobs."""
    store = _cron_store(args)
    jobs = store.list()

    if not jobs:
        print("No cron jobs.")
        return 0

    print(f"{'ID':<14} {'Name':<25} {'Schedule':<22} {'Target':<10} {'Enabled'}")
    print("-" * 80)
    for job in jobs:
        sched_str = _format_schedule(job.schedule)
        enabled = "yes" if job.enabled else "no"
        print(f"{job.id:<14} {job.name:<25} {sched_str:<22} {job.target:<10} {enabled}")

    print(f"\n{len(jobs)} job(s).")
    return 0


def cmd_cron_add(args: argparse.Namespace) -> int:
    """Add a new cron job."""
    from creel.cron.models import CronJob, Delivery, Payload, Schedule

    store = _cron_store(args)
    tz = getattr(args, "tz", "UTC") or "UTC"

    # Determine schedule
    if getattr(args, "cron", None):
        schedule = Schedule(kind="cron", expr=args.cron, tz=tz)
    elif getattr(args, "every", None):
        schedule = Schedule(kind="every", expr=str(args.every), tz=tz)
    elif getattr(args, "at", None):
        schedule = Schedule(kind="at", expr=args.at, tz=tz)
    else:
        print("Error: must specify --cron, --every, or --at", file=sys.stderr)
        return 1

    # Determine payload
    message = getattr(args, "message", None)
    system_event = getattr(args, "system_event", None)
    if message:
        payload = Payload(kind="agentTurn", message=message)
    elif system_event:
        payload = Payload(kind="systemEvent", message=system_event)
    else:
        print("Error: must specify --message or --system-event", file=sys.stderr)
        return 1

    if getattr(args, "model", None):
        payload.model = args.model
    timeout = getattr(args, "timeout_seconds", None)
    if timeout is not None:
        payload.timeout_seconds = timeout

    # Determine target
    target_raw = getattr(args, "target", "isolated") or "isolated"
    if system_event:
        target_raw = "main"
    target = cast(Literal["main", "isolated"], target_raw)

    # Determine delivery
    delivery_mode_raw = getattr(args, "delivery_mode", "none") or "none"
    delivery_mode = cast(Literal["announce", "webhook", "none"], delivery_mode_raw)
    delivery = Delivery(
        mode=delivery_mode,
        channel=getattr(args, "delivery_channel", None),
        url=getattr(args, "delivery_url", None),
    )

    try:
        job = CronJob(
            name=args.name,
            schedule=schedule,
            target=target,
            payload=payload,
            delivery=delivery,
            enabled=not getattr(args, "disabled", False),
        )
        store.add(job)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Created job '{job.name}' ({job.id}).")
    return 0


def cmd_cron_edit(args: argparse.Namespace) -> int:
    """Edit an existing cron job."""
    from creel.cron.models import Schedule

    store = _cron_store(args)
    job = store.get(args.job_id)
    if job is None:
        print(f"Error: Job '{args.job_id}' not found", file=sys.stderr)
        return 1

    fields: dict = {}

    if getattr(args, "name", None):
        fields["name"] = args.name

    # Schedule update
    tz = getattr(args, "tz", None) or job.schedule.tz
    if getattr(args, "cron", None):
        fields["schedule"] = Schedule(kind="cron", expr=args.cron, tz=tz).model_dump()
    elif getattr(args, "every", None):
        fields["schedule"] = Schedule(kind="every", expr=str(args.every), tz=tz).model_dump()
    elif getattr(args, "at", None):
        fields["schedule"] = Schedule(kind="at", expr=args.at, tz=tz).model_dump()

    if getattr(args, "enable", False):
        fields["enabled"] = True
    elif getattr(args, "disable", False):
        fields["enabled"] = False

    if not fields:
        print("No changes specified.")
        return 0

    try:
        updated = store.update(args.job_id, **fields)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Updated job '{updated.name}' ({updated.id}).")
    return 0


def cmd_cron_remove(args: argparse.Namespace) -> int:
    """Remove a cron job."""
    store = _cron_store(args)
    try:
        job = store.remove(args.job_id)
    except KeyError:
        print(f"Error: Job '{args.job_id}' not found", file=sys.stderr)
        return 1

    print(f"Removed job '{job.name}' ({job.id}).")
    return 0


def cmd_cron_run(args: argparse.Namespace) -> int:
    """Trigger a cron job immediately."""
    from creel.cron.executor import JobExecutor
    from creel.cron.manager import CronManager
    from creel.cron.models import RunStatus

    store = _cron_store(args)
    job = store.get(args.job_id)
    if job is None:
        print(f"Error: Job '{args.job_id}' not found", file=sys.stderr)
        return 1

    # Set up executor: main-session jobs print to stdout, isolated need agent config
    def cli_inject_event(text: str) -> None:
        print(text)

    agent_def = None
    try:
        agent_def = _load_agent_def(args)
    except Exception:
        pass

    executor = JobExecutor(
        agent_def=agent_def,
        inject_event=cli_inject_event,
    )
    manager = CronManager(store=store, executor=executor)

    print(f"Running job '{job.name}' ({args.job_id})...")
    manager.trigger_job(args.job_id)

    # Check latest run record for status
    runs = store.get_runs(args.job_id)
    if runs:
        latest = runs[-1]
        if latest.status == RunStatus.FAILURE:
            print(f"Job failed: {latest.error}", file=sys.stderr)
            return 1

    print(f"Job '{job.name}' completed successfully.")
    return 0


def cmd_cron_runs(args: argparse.Namespace) -> int:
    """Show run history for a cron job."""
    store = _cron_store(args)
    job = store.get(args.job_id)
    if job is None:
        print(f"Error: Job '{args.job_id}' not found", file=sys.stderr)
        return 1

    runs = store.get_runs(args.job_id)
    if not runs:
        print(f"No runs recorded for '{job.name}' ({args.job_id}).")
        return 0

    tail = getattr(args, "tail", 20)
    if tail and tail < len(runs):
        runs = runs[-tail:]

    print(f"Run history for '{job.name}' ({args.job_id}):")
    from datetime import datetime

    print(f"{'Started':<28} {'Status':<10} {'Duration':<12} {'Error'}")
    print("-" * 70)
    for run in runs:
        duration = ""
        if run.ended_at and run.started_at:
            try:
                start = datetime.fromisoformat(run.started_at)
                end = datetime.fromisoformat(run.ended_at)
                secs = (end - start).total_seconds()
                duration = f"{secs:.1f}s"
            except ValueError:
                pass
        error = run.error or ""
        print(f"{run.started_at:<28} {run.status.value:<10} {duration:<12} {error}")

    print(f"\n{len(runs)} run(s) shown.")
    return 0


def _get_kb(args: argparse.Namespace) -> KnowledgeBase:
    """Create a KnowledgeBase instance from agent config."""
    from creel.knowledge_base import KnowledgeBase

    try:
        agent_def = _load_agent_def(args)
        kb_config = agent_def.knowledge_base
        workspace = Path(agent_def.workspace.path)
        db_path = kb_config.db_path or str(workspace / ".kb_index.sqlite")
    except FileNotFoundError:
        db_path = "workspace/.kb_index.sqlite"
        kb_config = None

    chunk_size = kb_config.chunk_size if kb_config else 512
    chunk_overlap = kb_config.chunk_overlap if kb_config else 50
    embedding_model = kb_config.embedding_model if kb_config else "all-MiniLM-L6-v2"

    return KnowledgeBase(
        db_path=db_path,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        embedding_model=embedding_model,
    )


def cmd_kb_add(args: argparse.Namespace) -> int:
    """Add a file or directory to the knowledge base."""
    from creel.tools import _is_kb_path_safe

    if not _is_kb_path_safe(args.path):
        print(f"Error: refused to index sensitive path: {args.path}", file=sys.stderr)
        return 1

    kb = _get_kb(args)
    try:
        result = kb.add(args.path)
        added = result["added"]
        updated = result["updated"]
        skipped = result["skipped"]
        errors = result["errors"]

        if added or updated:
            files = result.get("files", [])
            if files:
                for f in files:
                    print(f"  indexed: {f}")
            print(f"\nAdded: {added}  Updated: {updated}  Skipped: {skipped}  Errors: {errors}")
        elif errors:
            print(f"Errors: {errors}  (no files indexed)")
            return 1
        else:
            print("No new files to index.")
        return 0
    finally:
        kb.close()


def cmd_kb_search(args: argparse.Namespace) -> int:
    """Search the knowledge base."""
    kb = _get_kb(args)
    try:
        results = kb.search(args.query, top_k=args.top_k)
        if not results:
            print(f"No results found for '{args.query}'.")
            return 0

        for i, r in enumerate(results, 1):
            source = r["source"]
            title = r["title"]
            score = r["score"]
            content = r["content"]
            # Truncate long content for display
            if len(content) > 300:
                content = content[:300] + "..."
            print(f"\n--- Result {i} (score: {score:.4f}) ---")
            print(f"Source: {source}")
            print(f"Title: {title}")
            print(content)

        print(f"\n{len(results)} result(s) found.")
        return 0
    finally:
        kb.close()


def cmd_kb_list(args: argparse.Namespace) -> int:
    """List indexed documents."""
    kb = _get_kb(args)
    try:
        docs = kb.list_documents()
        if not docs:
            print("Knowledge base is empty.")
            return 0

        for doc in docs:
            chunks = doc["chunks"]
            size = doc["size"]
            title = doc["title"]
            path = doc["path"]
            print(f"  {title}  ({chunks} chunks, {size} bytes)")
            print(f"    {path}")

        print(f"\n{len(docs)} document(s) indexed.")
        return 0
    finally:
        kb.close()


def cmd_kb_remove(args: argparse.Namespace) -> int:
    """Remove a document from the knowledge base."""
    kb = _get_kb(args)
    try:
        result = kb.remove(args.path)
        if result["removed"]:
            print(f"Removed: {result['path']}")
        else:
            print(f"Not found: {result.get('error', result['path'])}")
            return 1
        return 0
    finally:
        kb.close()


def cmd_kb_rebuild(args: argparse.Namespace) -> int:
    """Rebuild the knowledge base index."""
    kb = _get_kb(args)
    try:
        print("Rebuilding knowledge base index...")
        result = kb.rebuild()
        added = result.get("added", 0)
        errors = result.get("errors", 0)
        print(f"Rebuilt: {added} document(s) re-indexed, {errors} error(s).")
        return 0
    finally:
        kb.close()


def cmd_kb_stats(args: argparse.Namespace) -> int:
    """Show knowledge base statistics."""
    kb = _get_kb(args)
    try:
        s = kb.stats()
        print(f"Documents:   {s['documents']}")
        print(f"Chunks:      {s['chunks']}")
        print(f"Total size:  {s['total_size']} bytes")
        print(f"DB size:     {s['db_size']} bytes")
        return 0
    finally:
        kb.close()


def cmd_audit(args: argparse.Namespace) -> int:
    """Query the guardian audit log."""

    from guardian.audit import read_audit_log

    try:
        agent_def = _load_agent_def(args)
    except FileNotFoundError:
        print(
            f"Error: Agent config not found at {args.agent_config or _default_agent_config()}",
            file=sys.stderr,
        )
        return 1

    log_file = (
        agent_def.guardian.audit.log_file
        if (agent_def.guardian and agent_def.guardian.audit.log_file)
        else str(paths.audit_log())
    )
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


def cmd_encrypt(args: argparse.Namespace) -> int:
    """Encrypt a plaintext .env file with age."""
    from creel.secrets import encrypt_env_file

    env_path = Path(args.env_file)
    try:
        output_path = encrypt_env_file(
            env_path=env_path,
            recipient_path=args.recipient,
            output_path=args.output,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error encrypting: {exc}", file=sys.stderr)
        return 1

    print(f"Encrypted: {env_path} -> {output_path}")

    if args.delete:
        env_path.unlink()
        print(f"Deleted plaintext: {env_path}")
    else:
        print(f"\nIMPORTANT: Delete the plaintext file:\n  rm {env_path}")

    return 0


def _init_rate_limiter(args: argparse.Namespace) -> None:
    """Initialize the global rate limiter from agent config."""
    from creel.models import load_agent_config
    from creel.rate_limiter import configure_rate_limiter

    try:
        agent_cfg = load_agent_config(
            getattr(args, "agent_config", None) or _default_agent_config()
        )
    except FileNotFoundError:
        logger.debug("Agent config not found, skipping rate limiter init")
        return

    rl = agent_cfg.llm.rate_limits
    if not rl.enabled:
        return

    usage_dir = paths.creel_home() / "usage"
    configure_rate_limiter(
        requests_per_minute=rl.requests_per_minute,
        requests_per_hour=rl.requests_per_hour,
        tokens_per_day=rl.tokens_per_day,
        cost_per_day_usd=rl.cost_per_day_usd,
        queue_timeout=rl.queue_timeout,
        usage_dir=usage_dir,
    )


def cmd_usage(args: argparse.Namespace) -> int:
    """Show current LLM usage statistics."""

    _init_rate_limiter(args)

    from creel.rate_limiter import get_rate_limiter

    limiter = get_rate_limiter()
    if limiter is None:
        print("Rate limiting is not enabled. Check agent.yaml (llm.rate_limits.enabled).")
        return 1

    if getattr(args, "history", False):
        days = getattr(args, "days", 7)
        history = limiter.get_usage_history(days=days)
        print(f"{'Date':<12} {'Requests':>8} {'Tokens':>10} {'Cost (USD)':>10}")
        print("-" * 44)
        total_cost = 0.0
        total_requests = 0
        total_tokens = 0
        for day in history:
            total_cost += day["cost_usd"]
            total_requests += day["requests"]
            total_tokens += day["total_tokens"]
            print(
                f"{day['date']:<12} {day['requests']:>8} "
                f"{day['total_tokens']:>10} ${day['cost_usd']:>9.4f}"
            )
        print("-" * 44)
        print(f"{'Total':<12} {total_requests:>8} {total_tokens:>10} ${total_cost:>9.4f}")
        return 0

    usage = limiter.get_usage()
    print("Current LLM Usage:")
    print(
        f"  Requests (last minute): {usage.requests_last_minute}"
        f" / {usage.requests_per_minute_limit}"
    )
    print(f"  Requests (last hour):   {usage.requests_last_hour} / {usage.requests_per_hour_limit}")
    print(f"  Tokens (today):         {usage.tokens_today:,} / {usage.tokens_per_day_limit:,}")
    print(
        f"  Cost (today):           ${usage.cost_today_usd:.4f}"
        f" / ${usage.cost_per_day_limit_usd:.2f}"
    )
    if usage.override_active:
        import datetime

        exp = datetime.datetime.fromtimestamp(usage.override_expires_at or 0, tz=datetime.UTC)
        print(f"  Override active until:  {exp.isoformat()}")
    return 0


def cmd_limits_override(args: argparse.Namespace) -> int:
    """Temporarily override rate limits."""
    _init_rate_limiter(args)

    from creel.rate_limiter import get_rate_limiter

    limiter = get_rate_limiter()
    if limiter is None:
        print("Rate limiting is not enabled. Check agent.yaml (llm.rate_limits.enabled).")
        return 1

    duration_str = args.duration
    # Parse duration string like "1h", "30m", "2h"
    multipliers = {"s": 1, "m": 60, "h": 3600}
    unit = duration_str[-1].lower()
    if unit not in multipliers:
        print(f"Invalid duration unit '{unit}'. Use s, m, or h (e.g., '1h', '30m').")
        return 1
    try:
        value = float(duration_str[:-1])
    except ValueError:
        print(f"Invalid duration value: {duration_str}")
        return 1

    if value <= 0:
        print("Duration must be positive.")
        return 1

    seconds = value * multipliers[unit]
    limiter.override(seconds)
    print(f"Rate limits overridden for {duration_str} ({seconds:.0f} seconds).")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run installation health checks."""
    from creel.doctor import run_doctor

    agent_config_path = args.agent_config
    report = run_doctor(
        agent_config_path=agent_config_path,
        fix=args.fix,
        no_color=args.no_color,
    )
    return 1 if report.errors > 0 else 0


# ---------------------------------------------------------------------------
# monitor subcommands
# ---------------------------------------------------------------------------


def _monitor_store(args: argparse.Namespace):
    """Create a MonitorStore from default or overridden paths."""
    from creel.monitors.store import MonitorStore

    mon_dir = Path(getattr(args, "monitors_dir", paths.monitors_dir()))
    return MonitorStore(
        monitors_path=mon_dir / "monitors.json",
        runs_path=mon_dir / "runs.json",
        alerts_path=mon_dir / "alerts.json",
    )


def cmd_monitor_list(args: argparse.Namespace) -> int:
    """List all monitors."""
    store = _monitor_store(args)
    monitors = store.list()

    if not monitors:
        print("No monitors configured.")
        return 0

    print(f"{'ID':<14} {'Name':<28} {'Schedule':<18} {'Level':<8} {'Enabled'}")
    print("-" * 80)
    for mon in monitors:
        sched_str = _format_schedule(mon.schedule)
        enabled = "yes" if mon.enabled else "no"
        print(f"{mon.id:<14} {mon.name:<28} {sched_str:<18} {mon.alert_level.value:<8} {enabled}")

    print(f"\n{len(monitors)} monitor(s).")
    return 0


def cmd_monitor_add(args: argparse.Namespace) -> int:
    """Add a new monitor."""
    from creel.cron.models import Delivery, Schedule
    from creel.monitors.models import AlertLevel, Monitor, QuietHours

    store = _monitor_store(args)
    tz = getattr(args, "tz", "UTC") or "UTC"

    # Schedule
    cron_expr = getattr(args, "cron", None)
    every = getattr(args, "every", None)
    if cron_expr:
        schedule = Schedule(kind="cron", expr=cron_expr, tz=tz)
    elif every:
        schedule = Schedule(kind="every", expr=str(every), tz=tz)
    else:
        print("Error: must specify --cron or --every", file=sys.stderr)
        return 1

    # Delivery
    delivery_channel = getattr(args, "delivery_channel", None)
    delivery_url = getattr(args, "delivery_url", None)
    if delivery_channel:
        delivery = Delivery(mode="announce", channel=delivery_channel)
    elif delivery_url:
        delivery = Delivery(mode="webhook", url=delivery_url)
    else:
        delivery = Delivery(mode="none")

    # Alert level
    level_str = getattr(args, "alert_level", "notice") or "notice"
    try:
        alert_level = AlertLevel(level_str)
    except ValueError:
        print(f"Error: invalid alert level '{level_str}'", file=sys.stderr)
        return 1

    # Quiet hours
    quiet_hours = None
    qh_str = getattr(args, "quiet_hours", None)
    if qh_str:
        try:
            start, end = qh_str.split("-")
            quiet_hours = QuietHours(start=start.strip(), end=end.strip(), timezone=tz)
        except (ValueError, IndexError):
            print(
                "Error: --quiet-hours must be 'HH:MM-HH:MM' format",
                file=sys.stderr,
            )
            return 1

    try:
        monitor = Monitor(
            name=args.name,
            description=getattr(args, "description", "") or "",
            schedule=schedule,
            executor=args.executor,
            prompt=args.prompt,
            delivery=delivery,
            alert_level=alert_level,
            quiet_hours=quiet_hours,
            cooldown_seconds=getattr(args, "cooldown", 3600) or 3600,
            enabled=not getattr(args, "disabled", False),
        )
        store.add(monitor)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Created monitor '{monitor.name}' ({monitor.id}).")
    return 0


def cmd_monitor_add_template(args: argparse.Namespace) -> int:
    """Add a monitor from a built-in template."""
    from creel.cron.models import Delivery
    from creel.monitors.templates import create_from_template

    store = _monitor_store(args)

    delivery_channel = getattr(args, "delivery_channel", None)
    delivery_url = getattr(args, "delivery_url", None)
    if delivery_channel:
        delivery = Delivery(mode="announce", channel=delivery_channel)
    elif delivery_url:
        delivery = Delivery(mode="webhook", url=delivery_url)
    else:
        delivery = None

    tz = getattr(args, "tz", None)

    try:
        monitor = create_from_template(
            args.template_name,
            delivery=delivery,
            quiet_hours_tz=tz,
        )
        store.add(monitor)
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Created monitor '{monitor.name}' ({monitor.id}) from template '{args.template_name}'.")
    return 0


def cmd_monitor_templates(args: argparse.Namespace) -> int:
    """List available monitor templates."""
    from creel.monitors.templates import TEMPLATES

    if not TEMPLATES:
        print("No templates available.")
        return 0

    for name, tmpl in TEMPLATES.items():
        print(f"  {name:<22} {tmpl.get('description', '')}")

    print(f"\n{len(TEMPLATES)} template(s). Use 'creel monitor add-template <name>' to create.")
    return 0


def cmd_monitor_enable(args: argparse.Namespace) -> int:
    """Enable a monitor."""
    store = _monitor_store(args)
    try:
        mon = store.update(args.monitor_id, enabled=True)
    except KeyError:
        print(f"Error: Monitor '{args.monitor_id}' not found", file=sys.stderr)
        return 1
    print(f"Enabled monitor '{mon.name}' ({mon.id}).")
    return 0


def cmd_monitor_disable(args: argparse.Namespace) -> int:
    """Disable a monitor."""
    store = _monitor_store(args)
    try:
        mon = store.update(args.monitor_id, enabled=False)
    except KeyError:
        print(f"Error: Monitor '{args.monitor_id}' not found", file=sys.stderr)
        return 1
    print(f"Disabled monitor '{mon.name}' ({mon.id}).")
    return 0


def cmd_monitor_remove(args: argparse.Namespace) -> int:
    """Remove a monitor."""
    store = _monitor_store(args)
    try:
        mon = store.remove(args.monitor_id)
    except KeyError:
        print(f"Error: Monitor '{args.monitor_id}' not found", file=sys.stderr)
        return 1
    print(f"Removed monitor '{mon.name}' ({mon.id}).")
    return 0


def cmd_monitor_run(args: argparse.Namespace) -> int:
    """Trigger a monitor check immediately."""
    from creel.monitors.manager import MonitorManager
    from creel.monitors.models import MonitorRunStatus

    store = _monitor_store(args)
    mon = store.get(args.monitor_id)
    if mon is None:
        print(f"Error: Monitor '{args.monitor_id}' not found", file=sys.stderr)
        return 1

    def cli_executor(monitor):
        """Simple CLI executor that prints the prompt and returns None (no alert)."""
        print(f"[Check] {monitor.prompt}")
        return None

    manager = MonitorManager(store=store, executor=cli_executor)

    print(f"Running monitor '{mon.name}' ({args.monitor_id})...")
    record = manager.trigger_monitor(args.monitor_id)

    status_icons = {
        MonitorRunStatus.OK: "OK (nothing to report)",
        MonitorRunStatus.ALERTED: "ALERTED",
        MonitorRunStatus.SUPPRESSED: f"SUPPRESSED ({record.suppressed_reason})",
        MonitorRunStatus.FAILURE: f"FAILED: {record.error}",
    }
    print(f"Result: {status_icons.get(record.status, record.status)}")
    return 1 if record.status == MonitorRunStatus.FAILURE else 0


def cmd_monitor_history(args: argparse.Namespace) -> int:
    """Show alert and run history for a monitor."""
    store = _monitor_store(args)
    mon = store.get(args.monitor_id)
    if mon is None:
        print(f"Error: Monitor '{args.monitor_id}' not found", file=sys.stderr)
        return 1

    show_type = getattr(args, "type", "all") or "all"
    tail = getattr(args, "tail", 20)

    if show_type in ("all", "runs"):
        runs = store.get_runs(args.monitor_id)
        if tail and tail < len(runs):
            runs = runs[-tail:]

        if runs:
            print(f"Run history for '{mon.name}':")
            print(f"{'Started':<28} {'Status':<12} {'Level':<8} {'Detail'}")
            print("-" * 75)
            for run in runs:
                detail = ""
                if run.suppressed_reason:
                    detail = f"suppressed: {run.suppressed_reason}"
                elif run.error:
                    detail = run.error
                level = run.alert_level.value if run.alert_level else ""
                print(f"{run.started_at:<28} {run.status.value:<12} {level:<8} {detail}")
            print(f"\n{len(runs)} run(s) shown.")
        else:
            print(f"No runs recorded for '{mon.name}'.")

    if show_type in ("all", "alerts"):
        alerts = store.get_alerts(args.monitor_id)
        if tail and tail < len(alerts):
            alerts = alerts[-tail:]

        if alerts:
            if show_type == "all":
                print()
            print(f"Alert history for '{mon.name}':")
            print(f"{'Timestamp':<28} {'Level':<8} {'Delivered':<10} {'Message'}")
            print("-" * 80)
            for alert in alerts:
                delivered = "yes" if alert.delivered else f"no ({alert.suppressed_reason})"
                msg = alert.message[:50] + "..." if len(alert.message) > 50 else alert.message
                print(f"{alert.timestamp:<28} {alert.level.value:<8} {delivered:<10} {msg}")
            print(f"\n{len(alerts)} alert(s) shown.")
        else:
            print(f"No alerts recorded for '{mon.name}'.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="creel",
        description="LLM Task Runner - secure, scheduled LLM task execution",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--containers", action="store_true", help="Run executors/LLM in Docker containers"
    )
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=None,
        help=f"Tasks directory (default: {_default_tasks_dir()})",
    )
    parser.add_argument(
        "--agent-config",
        type=Path,
        default=None,
        help=f"Path to agent.yaml config (default: {_default_agent_config()})",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="Output structured JSON log lines (for production)",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Disable the LLM judge (Stage 2) to save API calls during development",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init command
    init_parser = subparsers.add_parser("init", help="Scaffold ~/.creel/ directory")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    init_parser.add_argument(
        "--migrate", action="store_true", help="Copy existing repo config into ~/.creel/"
    )
    init_parser.add_argument(
        "--repo-root",
        type=str,
        default=None,
        help="Repo root to migrate from (default: current directory)",
    )
    init_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip interactive wizard, use CLI args",
    )
    init_parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "google", "ollama"],
        help="LLM provider",
    )
    init_parser.add_argument("--api-key", type=str, help="LLM API key")
    init_parser.add_argument("--model", type=str, help="LLM model name")
    init_parser.add_argument(
        "--channel",
        choices=["telegram", "imessage", "whatsapp", "none"],
        help="Communication channel",
    )
    init_parser.add_argument("--bot-token", type=str, help="Telegram bot token")
    init_parser.add_argument(
        "--allowed-senders",
        type=str,
        help="Comma-separated list of allowed sender usernames",
    )
    init_parser.add_argument(
        "--tools",
        type=str,
        help="Comma-separated list of tools to enable "
        "(gmail,calendar,drive,web_search,weather,github,notion,shell)",
    )
    init_parser.add_argument("--enable-media", action="store_true", help="Enable media processing")
    init_parser.add_argument(
        "--no-guardian", action="store_true", help="Disable guardian security pipeline"
    )

    # run command
    run_parser = subparsers.add_parser("run", help="Run a task immediately")
    run_parser.add_argument("task_name", help="Name of the task to run")
    run_parser.add_argument("--dry", action="store_true", help="Dry run (render prompt only)")

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
        default=None,
        help="Unix socket path (default: ~/.creel/daemon.sock)",
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
        "--event",
        type=str,
        default=None,
        help="Filter by event type (screen_input, validate_action, tool_result)",
    )
    audit_parser.add_argument("--all", action="store_true", help="Show all entries (no tail limit)")
    audit_parser.add_argument("--tool", type=str, default=None, help="Filter by tool name")
    audit_parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Show entries since date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
    )

    # doctor command
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check Creel installation health and configuration"
    )
    doctor_parser.add_argument("--fix", action="store_true", help="Auto-fix remediable issues")
    doctor_parser.add_argument("--no-color", action="store_true", help="Disable colorized output")

    # encrypt command
    encrypt_parser = subparsers.add_parser("encrypt", help="Encrypt a .env file with age")
    encrypt_parser.add_argument("env_file", help="Path to plaintext .env file")
    encrypt_parser.add_argument(
        "--recipient",
        default=None,
        help="Age recipient (public key) file (default: ~/.age/key.pub or AGE_RECIPIENT_FILE)",
    )
    encrypt_parser.add_argument(
        "--output",
        default=None,
        help="Output file path (default: <env_file>.enc)",
    )
    encrypt_parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the plaintext file after encryption",
    )

    # --- Shared daemon parent parsers (to avoid option duplication) ---
    _daemon_paths_parent = argparse.ArgumentParser(add_help=False)
    _daemon_paths_parent.add_argument(
        "--socket-path",
        type=Path,
        default=None,
        help="Unix socket path (default: ~/.creel/daemon.sock)",
    )
    _daemon_paths_parent.add_argument(
        "--pid-file",
        type=Path,
        default=None,
        help="PID file path (default: ~/.creel/daemon.pid)",
    )

    _daemon_launchd_parent = argparse.ArgumentParser(add_help=False)
    _daemon_launchd_parent.add_argument(
        "--label",
        default=DEFAULT_DAEMON_LABEL,
        help=f"launchd service label (default: {DEFAULT_DAEMON_LABEL})",
    )
    _daemon_launchd_parent.add_argument(
        "--plist-path",
        type=Path,
        default=None,
        help="launchd plist path (default: ~/Library/LaunchAgents/com.creel.daemon.plist)",
    )

    _daemon_runtime_parent = argparse.ArgumentParser(add_help=False)
    _daemon_runtime_parent.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Daemon log file (default: ~/.creel/daemon.log)",
    )
    _daemon_runtime_parent.add_argument(
        "--channel",
        dest="channel_type",
        default="imessage",
        help="Channel plugin to run inside daemon (default: imessage). Use 'none' to disable.",
    )
    _daemon_runtime_parent.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Disable scheduler in daemon runtime",
    )
    _daemon_runtime_parent.add_argument(
        "--wait-seconds",
        type=float,
        default=20.0,
        help="Seconds to wait for daemon health check (default: 20)",
    )
    _daemon_runtime_parent.add_argument(
        "--http-port",
        type=int,
        default=8100,
        help="Also listen on this TCP port for dashboard access (default: 8100). Use 0 to disable.",
    )

    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Manage background daemon")
    daemon_subparsers = daemon_parser.add_subparsers(
        dest="daemon_command",
        metavar="{start,stop,restart,status,install,uninstall}",
    )

    daemon_subparsers.add_parser(
        "start",
        help="Start the background daemon",
        parents=[_daemon_paths_parent, _daemon_launchd_parent, _daemon_runtime_parent],
    )

    daemon_stop = daemon_subparsers.add_parser(
        "stop",
        help="Stop the background daemon",
        parents=[_daemon_paths_parent, _daemon_launchd_parent],
    )
    daemon_stop.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Stop timeout in seconds (default: 10)",
    )

    daemon_restart = daemon_subparsers.add_parser(
        "restart",
        help="Stop then start the daemon",
        parents=[_daemon_paths_parent, _daemon_launchd_parent, _daemon_runtime_parent],
    )
    daemon_restart.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Stop timeout in seconds (default: 10)",
    )

    daemon_subparsers.add_parser(
        "status",
        help="Show daemon status",
        parents=[_daemon_paths_parent, _daemon_launchd_parent],
    )

    daemon_subparsers.add_parser(
        "install",
        help="Install daemon as a launchd service",
        parents=[_daemon_paths_parent, _daemon_launchd_parent, _daemon_runtime_parent],
    )

    daemon_subparsers.add_parser(
        "uninstall",
        help="Uninstall daemon launchd service",
        parents=[_daemon_paths_parent, _daemon_launchd_parent],
    )

    # Internal foreground command used by `daemon start`
    daemon_run = daemon_subparsers.add_parser(
        "run",
        help=argparse.SUPPRESS,
        parents=[_daemon_paths_parent],
    )
    daemon_run.add_argument(
        "--channel",
        dest="channel_type",
        default="imessage",
    )
    daemon_run.add_argument("--no-scheduler", action="store_true")
    daemon_run.add_argument("--http-port", type=int, default=8100)
    daemon_subparsers._choices_actions = [  # type: ignore[attr-defined]
        action
        for action in daemon_subparsers._choices_actions  # type: ignore[attr-defined]
        if action.dest != "run"
    ]

    # reload command
    reload_parser = subparsers.add_parser("reload", help="Hot-reload daemon config without restart")
    reload_parser.add_argument(
        "--socket-path",
        type=Path,
        default=None,
        help="Unix socket path (default: ~/.creel/daemon.sock)",
    )

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
        default=None,
        help="Unix socket path (default: ~/.creel/daemon.sock)",
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
    send_parser.add_argument(
        "--auto-approve",
        action="store_true",
        default=False,
        help="Auto-approve Guardian REVIEW actions instead of queuing them",
    )

    # cron command group
    cron_parser = subparsers.add_parser("cron", help="Manage scheduled cron jobs")
    cron_subparsers = cron_parser.add_subparsers(
        dest="cron_command",
        metavar="{list,add,edit,remove,run,runs}",
    )

    # cron list
    cron_subparsers.add_parser("list", help="List all cron jobs")

    # cron add
    cron_add_parser = cron_subparsers.add_parser("add", help="Add a new cron job")
    cron_add_parser.add_argument("--name", required=True, help="Job name")
    cron_add_parser.add_argument("--cron", help="Cron expression (e.g., '0 8 * * *')")
    cron_add_parser.add_argument("--every", type=int, help="Interval in seconds")
    cron_add_parser.add_argument("--at", help="One-shot ISO 8601 timestamp")
    cron_add_parser.add_argument("--message", help="Agent turn message")
    cron_add_parser.add_argument("--system-event", help="System event message (main session)")
    cron_add_parser.add_argument("--model", help="Model override")
    cron_add_parser.add_argument("--timeout-seconds", type=int, help="Timeout in seconds")
    cron_add_parser.add_argument(
        "--target",
        choices=["main", "isolated"],
        default="isolated",
        help="Execution target (default: isolated)",
    )
    cron_add_parser.add_argument(
        "--delivery-mode",
        choices=["announce", "webhook", "none"],
        default="none",
        help="Delivery mode (default: none)",
    )
    cron_add_parser.add_argument("--delivery-channel", help="Channel name for announce delivery")
    cron_add_parser.add_argument("--delivery-url", help="URL for webhook delivery")
    cron_add_parser.add_argument("--tz", default="UTC", help="Timezone (default: UTC)")
    cron_add_parser.add_argument(
        "--disabled", action="store_true", help="Create job in disabled state"
    )

    # cron edit
    cron_edit_parser = cron_subparsers.add_parser("edit", help="Edit a cron job")
    cron_edit_parser.add_argument("job_id", help="Job ID to edit")
    cron_edit_parser.add_argument("--name", help="New name")
    cron_edit_parser.add_argument("--cron", help="New cron expression")
    cron_edit_parser.add_argument("--every", type=int, help="New interval in seconds")
    cron_edit_parser.add_argument("--at", help="New one-shot timestamp")
    cron_edit_parser.add_argument("--tz", help="New timezone")
    cron_edit_parser.add_argument("--enable", action="store_true", help="Enable job")
    cron_edit_parser.add_argument("--disable", action="store_true", help="Disable job")

    # cron remove
    cron_remove_parser = cron_subparsers.add_parser("remove", help="Remove a cron job")
    cron_remove_parser.add_argument("job_id", help="Job ID to remove")

    # cron run
    cron_run_parser = cron_subparsers.add_parser("run", help="Trigger a job immediately")
    cron_run_parser.add_argument("job_id", help="Job ID to run")

    # cron runs
    cron_runs_parser = cron_subparsers.add_parser("runs", help="Show run history for a job")
    cron_runs_parser.add_argument("job_id", help="Job ID to show runs for")
    cron_runs_parser.add_argument(
        "--tail",
        type=int,
        default=20,
        help="Show last N runs (default: 20)",
    )

    # kb command group (knowledge base)
    kb_parser = subparsers.add_parser("kb", help="Manage the knowledge base")
    kb_subparsers = kb_parser.add_subparsers(
        dest="kb_command",
        metavar="{add,search,list,remove,rebuild,stats}",
    )

    # kb add
    kb_add_parser = kb_subparsers.add_parser("add", help="Index a file or directory")
    kb_add_parser.add_argument("path", help="Path to file or directory to index")

    # kb search
    kb_search_parser = kb_subparsers.add_parser("search", help="Search the knowledge base")
    kb_search_parser.add_argument("query", help="Search query")
    kb_search_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Max results to return (default: 5)",
    )

    # kb list
    kb_subparsers.add_parser("list", help="List indexed documents")

    # kb remove
    kb_remove_parser = kb_subparsers.add_parser("remove", help="Remove a document from the index")
    kb_remove_parser.add_argument("path", help="Path of document to remove")

    # kb rebuild
    kb_subparsers.add_parser("rebuild", help="Rebuild the entire index")

    # kb stats
    kb_subparsers.add_parser("stats", help="Show knowledge base statistics")

    # usage command
    usage_parser = subparsers.add_parser("usage", help="Show LLM usage statistics")
    usage_parser.add_argument(
        "--history",
        action="store_true",
        help="Show daily/weekly cost breakdown",
    )
    usage_parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days for history (default: 7)",
    )

    # limits command group
    limits_parser = subparsers.add_parser("limits", help="Manage rate limits")
    limits_subparsers = limits_parser.add_subparsers(
        dest="limits_command",
        metavar="{override}",
    )
    limits_override_parser = limits_subparsers.add_parser(
        "override", help="Temporarily override rate limits"
    )
    limits_override_parser.add_argument(
        "--duration",
        required=True,
        help="Override duration (e.g., '1h', '30m', '60s')",
    )

    # monitor command group
    monitor_parser = subparsers.add_parser("monitor", help="Manage proactive monitors and alerts")
    monitor_subparsers = monitor_parser.add_subparsers(
        dest="monitor_command",
        metavar="{list,add,add-template,templates,enable,disable,remove,run,history}",
    )

    # monitor list
    monitor_subparsers.add_parser("list", help="List all monitors")

    # monitor add
    mon_add_parser = monitor_subparsers.add_parser("add", help="Add a new monitor")
    mon_add_parser.add_argument("--name", required=True, help="Monitor name")
    mon_add_parser.add_argument(
        "--executor", required=True, help="Executor name (e.g., gmail_readonly)"
    )
    mon_add_parser.add_argument("--prompt", required=True, help="What to check for")
    mon_add_parser.add_argument("--cron", help="Cron expression (e.g., '*/15 * * * *')")
    mon_add_parser.add_argument("--every", type=int, help="Check interval in seconds")
    mon_add_parser.add_argument("--delivery-channel", help="Channel for alert delivery")
    mon_add_parser.add_argument("--delivery-url", help="Webhook URL for alert delivery")
    mon_add_parser.add_argument(
        "--alert-level",
        choices=["info", "notice", "urgent"],
        default="notice",
        help="Alert severity level (default: notice)",
    )
    mon_add_parser.add_argument("--quiet-hours", help="Quiet hours range (e.g., '23:00-07:00')")
    mon_add_parser.add_argument(
        "--cooldown", type=int, default=3600, help="Dedup cooldown in seconds (default: 3600)"
    )
    mon_add_parser.add_argument("--description", help="Monitor description")
    mon_add_parser.add_argument("--tz", default="UTC", help="Timezone (default: UTC)")
    mon_add_parser.add_argument(
        "--disabled", action="store_true", help="Create monitor in disabled state"
    )

    # monitor add-template
    mon_tmpl_parser = monitor_subparsers.add_parser(
        "add-template", help="Add a monitor from a built-in template"
    )
    mon_tmpl_parser.add_argument("template_name", help="Template name")
    mon_tmpl_parser.add_argument("--delivery-channel", help="Channel for alert delivery")
    mon_tmpl_parser.add_argument("--delivery-url", help="Webhook URL for alert delivery")
    mon_tmpl_parser.add_argument("--tz", help="Timezone for quiet hours")

    # monitor templates
    monitor_subparsers.add_parser("templates", help="List available monitor templates")

    # monitor enable
    mon_enable_parser = monitor_subparsers.add_parser("enable", help="Enable a monitor")
    mon_enable_parser.add_argument("monitor_id", help="Monitor ID")

    # monitor disable
    mon_disable_parser = monitor_subparsers.add_parser("disable", help="Disable a monitor")
    mon_disable_parser.add_argument("monitor_id", help="Monitor ID")

    # monitor remove
    mon_remove_parser = monitor_subparsers.add_parser("remove", help="Remove a monitor")
    mon_remove_parser.add_argument("monitor_id", help="Monitor ID")

    # monitor run
    mon_run_parser = monitor_subparsers.add_parser(
        "run", help="Trigger a monitor check immediately"
    )
    mon_run_parser.add_argument("monitor_id", help="Monitor ID")

    # monitor history
    mon_history_parser = monitor_subparsers.add_parser("history", help="Show run and alert history")
    mon_history_parser.add_argument("monitor_id", help="Monitor ID")
    mon_history_parser.add_argument(
        "--type",
        choices=["all", "runs", "alerts"],
        default="all",
        help="History type to show (default: all)",
    )
    mon_history_parser.add_argument(
        "--tail", type=int, default=20, help="Show last N entries (default: 20)"
    )

    args = parser.parse_args()

    # Set up logging
    from creel.log import setup_logging

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
            "restart": cmd_daemon_restart,
            "status": cmd_daemon_status,
            "install": cmd_daemon_install,
            "uninstall": cmd_daemon_uninstall,
            "run": cmd_daemon_run,
        }
        if args.daemon_command not in daemon_commands:
            daemon_parser.print_help()
            return 1
        return daemon_commands[args.daemon_command](args)

    if args.command == "cron":
        cron_commands = {
            "list": cmd_cron_list,
            "add": cmd_cron_add,
            "edit": cmd_cron_edit,
            "remove": cmd_cron_remove,
            "run": cmd_cron_run,
            "runs": cmd_cron_runs,
        }
        if args.cron_command not in cron_commands:
            cron_parser.print_help()
            return 1
        return cron_commands[args.cron_command](args)

    if args.command == "kb":
        kb_commands = {
            "add": cmd_kb_add,
            "search": cmd_kb_search,
            "list": cmd_kb_list,
            "remove": cmd_kb_remove,
            "rebuild": cmd_kb_rebuild,
            "stats": cmd_kb_stats,
        }
        if args.kb_command not in kb_commands:
            kb_parser.print_help()
            return 1
        return kb_commands[args.kb_command](args)

    if args.command == "limits":
        limits_commands = {
            "override": cmd_limits_override,
        }
        if args.limits_command not in limits_commands:
            limits_parser.print_help()
            return 1
        return limits_commands[args.limits_command](args)

    if args.command == "monitor":
        monitor_commands = {
            "list": cmd_monitor_list,
            "add": cmd_monitor_add,
            "add-template": cmd_monitor_add_template,
            "templates": cmd_monitor_templates,
            "enable": cmd_monitor_enable,
            "disable": cmd_monitor_disable,
            "remove": cmd_monitor_remove,
            "run": cmd_monitor_run,
            "history": cmd_monitor_history,
        }
        if args.monitor_command not in monitor_commands:
            monitor_parser.print_help()
            return 1
        return monitor_commands[args.monitor_command](args)

    commands = {
        "init": cmd_init,
        "run": cmd_run,
        "schedule": cmd_schedule,
        "list": cmd_list,
        "validate": cmd_validate,
        "attach": cmd_attach,
        "audit": cmd_audit,
        "doctor": cmd_doctor,
        "send": cmd_send,
        "encrypt": cmd_encrypt,
        "usage": cmd_usage,
        "reload": cmd_reload,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
