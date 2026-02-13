#!/usr/bin/env python3
"""LLM Task Runner - CLI entry point.

Usage:
    ./runner.py run <task_name>          Run a task immediately
    ./runner.py run <task_name> --dry    Dry run (render prompt, skip LLM/output)
    ./runner.py schedule                 Start scheduler for all tasks
    ./runner.py list                     List available tasks
    ./runner.py validate <task_name>     Validate a task definition
    ./runner.py chat                     Interactive CLI chat with agent
    ./runner.py listen                   Listen for iMessages and respond
    ./runner.py serve                    Listen for iMessages + run scheduler
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from taskrunner.models import load_all_tasks, load_task
from taskrunner.orchestrator import run_task
from taskrunner.secrets import parse_env_file
from taskrunner.scheduler import start_scheduler

DEFAULT_TASKS_DIR = Path("tasks")
DEFAULT_AGENT_CONFIG = Path("agent.yaml")


def _tasks_dir(args: argparse.Namespace) -> Path:
    return args.tasks_dir


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

    print(f"{'Name':<25} {'Schedule':<20} {'Fetchers':<30} {'Output'}")
    print("-" * 90)
    for task in tasks:
        fetchers = ", ".join(task.fetch.keys())
        print(f"{task.name:<25} {task.schedule:<20} {fetchers:<30} {task.output.type}:{task.output.to}")

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
        print(f"  Fetchers: {', '.join(task.fetch.keys())}")
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
    return load_agent_config(config_path)


def cmd_chat(args: argparse.Namespace) -> int:
    """Start interactive CLI chat."""
    from taskrunner.chat import ChatServer
    from taskrunner.session import SessionManager

    try:
        agent_def = _load_agent_def(args)
    except FileNotFoundError:
        print(f"Error: Agent config not found at {args.agent_config}", file=sys.stderr)
        return 1

    # Load LLM secrets early
    if agent_def.llm.secrets:
        from taskrunner.orchestrator import _load_secrets_to_env
        _load_secrets_to_env(agent_def.llm.secrets)

    # Handle --list-sessions: print and exit
    if args.list_sessions:
        from datetime import datetime, timezone

        mgr = SessionManager(
            sessions_dir=agent_def.session.sessions_dir,
            max_history=agent_def.session.max_history,
        )
        sessions = mgr.list_sessions("cli")
        if not sessions:
            print("No sessions found.")
            return 0
        active_id = mgr._get_active_session_id("cli")
        print(f"{'ID':<12} {'Title':<40} {'Messages':>8}  {'Last Active'}")
        print("-" * 80)
        for s in sessions:
            marker = " *" if s["session_id"] == active_id else ""
            dt = datetime.fromtimestamp(s["last_active"], tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
            title = (s["title"] or "(untitled)")[:38]
            print(f"{s['session_id']}{marker:<{12 - len(s['session_id'])}} {title:<40} {s['message_count']:>8}  {date_str}")
        return 0

    # -- TUI mode (default) --
    if not args.simple:
        try:
            from taskrunner.tui import ChatApp, _make_tui_confirm_fn
        except ImportError:
            args.simple = True  # fall back if textual not installed

    if not args.simple:
        # Suppress console log handlers — Textual owns the terminal
        root = logging.getLogger()
        for handler in root.handlers[:]:
            if isinstance(handler, logging.StreamHandler):
                root.removeHandler(handler)

        server = ChatServer(agent_def, use_containers=args.containers)
        app = ChatApp(server)
        server._confirm_fn = _make_tui_confirm_fn(app)

        if args.new:
            server._session_mgr.new_session("cli")
        if args.resume:
            try:
                server._session_mgr.resume_session("cli", args.resume)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1

        app.run()
        return 0

    # -- Simple stdin/stdout mode --
    from taskrunner.channels.stdin import StdinChannel

    def _confirm_action(tool_name: str, tool_input: dict, reason: str) -> bool:
        print(f"\n⚠ Guardian review: {tool_name}({tool_input})")
        print(f"  Reason: {reason}")
        try:
            answer = input("  Allow? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    server = ChatServer(agent_def, use_containers=args.containers, confirm_fn=_confirm_action)

    if args.new:
        session = server._session_mgr.new_session("cli")
        print(f"Started new session {session.session_id}.")

    if args.resume:
        try:
            session = server._session_mgr.resume_session("cli", args.resume)
            title = session.title or "(untitled)"
            print(f"Resumed session {args.resume}: {title}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    channel = StdinChannel()

    try:
        channel.listen(server.handle_message)
    except KeyboardInterrupt:
        print("\nChat stopped.")

    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    """Start iMessage listener."""
    from taskrunner.channels.imessage import IMessageChannel
    from taskrunner.chat import ChatServer

    try:
        agent_def = _load_agent_def(args)
    except FileNotFoundError:
        print(f"Error: Agent config not found at {args.agent_config}", file=sys.stderr)
        return 1

    if not agent_def.channels.imessage:
        print("Error: No imessage channel configured in agent.yaml", file=sys.stderr)
        return 1

    # Load LLM secrets early
    if agent_def.llm.secrets:
        from taskrunner.orchestrator import _load_secrets_to_env
        _load_secrets_to_env(agent_def.llm.secrets)

    server = ChatServer(agent_def, use_containers=args.containers)
    channel = IMessageChannel(
        allowed_senders=[agent_def.channels.imessage.listen_to],
        poll_interval=agent_def.channels.imessage.poll_interval,
    )

    print(f"Listening for iMessages from {agent_def.channels.imessage.listen_to}...")
    try:
        channel.listen(server.handle_message)
    except KeyboardInterrupt:
        print("\nListener stopped.")

    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start iMessage listener + scheduler (daemon mode)."""
    import threading

    from taskrunner.channels.imessage import IMessageChannel
    from taskrunner.chat import ChatServer

    try:
        agent_def = _load_agent_def(args)
    except FileNotFoundError:
        print(f"Error: Agent config not found at {args.agent_config}", file=sys.stderr)
        return 1

    if not agent_def.channels.imessage:
        print("Error: No imessage channel configured in agent.yaml", file=sys.stderr)
        return 1

    # Load LLM secrets early
    if agent_def.llm.secrets:
        from taskrunner.orchestrator import _load_secrets_to_env
        _load_secrets_to_env(agent_def.llm.secrets)

    # Start scheduler in a background thread
    tasks_dir = _tasks_dir(args)
    if tasks_dir.is_dir():
        def run_scheduler():
            try:
                start_scheduler(tasks_dir=tasks_dir, use_containers=args.containers)
            except Exception:
                logging.getLogger(__name__).exception("Scheduler crashed")

        sched_thread = threading.Thread(target=run_scheduler, daemon=True)
        sched_thread.start()
        print("Scheduler started in background.")

    # Start iMessage listener in foreground
    server = ChatServer(agent_def, use_containers=args.containers)
    channel = IMessageChannel(
        allowed_senders=[agent_def.channels.imessage.listen_to],
        poll_interval=agent_def.channels.imessage.poll_interval,
    )

    print(f"Listening for iMessages from {agent_def.channels.imessage.listen_to}...")
    try:
        channel.listen(server.handle_message)
    except KeyboardInterrupt:
        print("\nServer stopped.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="LLM Task Runner - secure, scheduled LLM task execution",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output"
    )
    parser.add_argument(
        "--containers", action="store_true", help="Run fetchers/LLM in Docker containers"
    )
    parser.add_argument(
        "--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR, help="Tasks directory"
    )
    parser.add_argument(
        "--agent-config", type=Path, default=DEFAULT_AGENT_CONFIG,
        help="Path to agent.yaml config",
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

    # chat command
    chat_parser = subparsers.add_parser("chat", help="Interactive CLI chat with agent")
    chat_parser.add_argument(
        "--new", action="store_true", help="Start a new session (don't resume the active one)"
    )
    chat_parser.add_argument(
        "--resume", metavar="ID", help="Resume a specific session by ID"
    )
    chat_parser.add_argument(
        "--list-sessions", action="store_true", help="List sessions and exit"
    )
    chat_parser.add_argument(
        "--simple", action="store_true",
        help="Use simple stdin/stdout mode instead of TUI",
    )

    # listen command
    subparsers.add_parser("listen", help="Listen for iMessages and respond")

    # serve command
    subparsers.add_parser("serve", help="Listen for iMessages + run scheduler")

    args = parser.parse_args()

    # Set up logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Load root .env if present (for PHONE, etc.)
    root_env = Path(".env")
    if root_env.exists():
        for key, value in parse_env_file(root_env).items():
            os.environ.setdefault(key, value)

    if args.command is None:
        parser.print_help()
        return 1

    commands = {
        "run": cmd_run,
        "schedule": cmd_schedule,
        "list": cmd_list,
        "validate": cmd_validate,
        "chat": cmd_chat,
        "listen": cmd_listen,
        "serve": cmd_serve,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
