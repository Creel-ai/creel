#!/usr/bin/env python3
"""Run local smoke checks for Creel.

This script combines:
1. command-based checks (pytest, CLI calls)
2. runtime behavioral checks (signal handling, backoff, config validation)
3. optional live checks (LLM + Docker)
4. optional manual confirmations

Outputs are written to `.smoke-runs/<timestamp>/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_FILE = Path(__file__).with_name("smoke_cases.yaml")
DEFAULT_RUNS_DIR = ROOT / ".smoke-runs"
DEFAULT_ONNX_MODEL = "protectai/deberta-v3-base-prompt-injection-v2"

# Ensure repo-root imports work regardless of caller CWD.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class CaseResult:
    case_id: str
    title: str
    phase: str
    status: str  # pass | fail | skip
    duration_s: float
    detail: str
    log_file: str = ""
    exit_code: int | None = None


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def output(self) -> str:
        return f"{self.stdout}{self.stderr}"


@dataclass
class SmokeContext:
    args: argparse.Namespace
    repo_root: Path
    runs_dir: Path
    run_dir: Path
    logs_dir: Path
    python_bin: str
    agent_config: Path
    docker_available: bool
    docker_reason: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_case_name(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id)


def _write_case_log(ctx: SmokeContext, case_id: str, content: str) -> Path:
    path = ctx.logs_dir / f"{_safe_case_name(case_id)}.log"
    path.write_text(content)
    return path


def _result(
    case: dict[str, Any],
    status: str,
    duration_s: float,
    detail: str,
    log_file: Path | None = None,
    exit_code: int | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case["id"],
        title=case["title"],
        phase=case["phase"],
        status=status,
        duration_s=duration_s,
        detail=detail,
        log_file=str(log_file) if log_file else "",
        exit_code=exit_code,
    )


def _deep_update(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _build_minimal_agent_config(
    ctx: SmokeContext,
    name: str,
    updates: dict[str, Any] | None = None,
) -> Path:
    sessions_dir = ctx.run_dir / "runtime" / name / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    base: dict[str, Any] = {
        "system_prompt": "You are a smoke-test assistant. Keep responses concise.",
        "tools": {},
        "llm": {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 128,
        },
        "agent": {
            "max_turns": 4,
        },
        "session": {
            "sessions_dir": str(sessions_dir),
            "max_history": 50,
            "summarize_on_trim": True,
            "ttl_hours": 0,
        },
        "workspace": {
            "path": "workspace",
            "timezone": "UTC",
            "memory_days": 1,
            "memory_max_chars": 1000,
            "max_chars_per_file": 5000,
        },
        "channels": {
            "bluebubbles": {
                "server_url": "http://127.0.0.1:9",
                "password": "smoke-password",
                "listen_to": ["+10000000000"],
                "poll_interval": 1,
            },
        },
        "guardian": {
            "enabled": False,
        },
    }

    if updates:
        _deep_update(base, updates)

    config_dir = ctx.run_dir / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"{name}.yaml"
    config_path.write_text(yaml.safe_dump(base, sort_keys=False))
    return config_path


def _format_command(raw_command: Any, ctx: SmokeContext) -> list[str]:
    vars_map = {
        "python": ctx.python_bin,
        "root": str(ctx.repo_root),
        "run_dir": str(ctx.run_dir),
        "agent_config": str(ctx.agent_config),
        "onnx_model": ctx.args.onnx_model,
    }

    if isinstance(raw_command, str):
        parts = shlex.split(raw_command)
    elif isinstance(raw_command, list):
        parts = [str(part) for part in raw_command]
    else:
        raise ValueError(f"Unsupported command type: {type(raw_command)}")

    return [part.format(**vars_map) for part in parts]


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> CommandResult:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_s=time.perf_counter() - started,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=command,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            duration_s=time.perf_counter() - started,
            timed_out=True,
        )


def _spawn_with_sigint(
    command: list[str],
    *,
    cwd: Path,
    wait_before_sigint_s: float,
    final_timeout_s: int,
    stdin_lines: list[str] | None = None,
    line_delay_s: float = 1.0,
    env: dict[str, str] | None = None,
) -> CommandResult:
    started = time.perf_counter()
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        if stdin_lines:
            for line in stdin_lines:
                if proc.poll() is not None:
                    break
                if proc.stdin is not None:
                    proc.stdin.write(f"{line}\n")
                    proc.stdin.flush()
                time.sleep(line_delay_s)

        if proc.poll() is None:
            time.sleep(wait_before_sigint_s)

        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)

        try:
            stdout, stderr = proc.communicate(timeout=final_timeout_s)
            return CommandResult(
                command=command,
                returncode=proc.returncode if proc.returncode is not None else 1,
                stdout=stdout,
                stderr=stderr,
                duration_s=time.perf_counter() - started,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return CommandResult(
                command=command,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                duration_s=time.perf_counter() - started,
                timed_out=True,
            )
    finally:
        if proc.poll() is None:
            proc.kill()


def _command_log_blob(result: CommandResult) -> str:
    cmd_str = " ".join(shlex.quote(part) for part in result.command)
    return (
        f"Command: {cmd_str}\n"
        f"Return code: {result.returncode}\n"
        f"Timed out: {result.timed_out}\n"
        f"Duration: {result.duration_s:.2f}s\n"
        f"\n=== STDOUT ===\n{result.stdout}\n"
        f"\n=== STDERR ===\n{result.stderr}\n"
    )


def _expected_exit_codes(case: dict[str, Any]) -> set[int]:
    expected = case.get("expected_exit", 0)
    if isinstance(expected, list):
        return {int(x) for x in expected}
    return {int(expected)}


def _check_output_expectations(case: dict[str, Any], output: str) -> tuple[bool, list[str]]:
    failures: list[str] = []

    contains = case.get("expect_output_contains", [])
    for expected in contains:
        if expected not in output:
            failures.append(f"missing expected output: {expected!r}")

    regexes = case.get("expect_output_regex", [])
    for pattern in regexes:
        if not re.search(pattern, output, flags=re.MULTILINE):
            failures.append(f"missing expected regex: {pattern!r}")

    return (len(failures) == 0, failures)


def _skip_result(case: dict[str, Any], detail: str) -> CaseResult:
    return _result(case, "skip", 0.0, detail)


def handle_command(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    command = _format_command(case["command"], ctx)
    timeout = int(case.get("timeout", ctx.args.timeout))
    stdin_text = case.get("stdin")

    result = _run_command(
        command,
        cwd=ctx.repo_root,
        timeout=timeout,
        env=os.environ.copy(),
        stdin_text=stdin_text,
    )
    output = result.output
    expected_codes = _expected_exit_codes(case)
    output_ok, output_failures = _check_output_expectations(case, output)
    code_ok = result.returncode in expected_codes

    log_path = _write_case_log(ctx, case["id"], _command_log_blob(result))

    if result.timed_out:
        return _result(
            case,
            "fail",
            time.perf_counter() - started,
            f"command timed out after {timeout}s",
            log_file=log_path,
            exit_code=result.returncode,
        )

    if code_ok and output_ok:
        return _result(
            case,
            "pass",
            time.perf_counter() - started,
            f"exit={result.returncode}",
            log_file=log_path,
            exit_code=result.returncode,
        )

    detail_parts: list[str] = []
    if not code_ok:
        detail_parts.append(f"unexpected exit={result.returncode} expected={sorted(expected_codes)}")
    detail_parts.extend(output_failures)

    return _result(
        case,
        "fail",
        time.perf_counter() - started,
        "; ".join(detail_parts),
        log_file=log_path,
        exit_code=result.returncode,
    )


def handle_manual(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    instructions = case.get("instructions", "").strip()

    if ctx.args.manual_ok:
        log_path = _write_case_log(
            ctx,
            case["id"],
            f"Manual check auto-passed via --manual-ok\n\n{instructions}\n",
        )
        return _result(
            case,
            "pass",
            time.perf_counter() - started,
            "manual check accepted via --manual-ok",
            log_file=log_path,
        )

    if not sys.stdin.isatty():
        return _skip_result(case, "manual check requires interactive terminal")

    print(f"\nManual check: {case['title']}")
    if instructions:
        print(instructions)

    answer = input("Result? [p]ass / [f]ail / [s]kip: ").strip().lower()
    if answer.startswith("p"):
        status = "pass"
        detail = "marked pass by operator"
    elif answer.startswith("f"):
        status = "fail"
        detail = "marked fail by operator"
    else:
        status = "skip"
        detail = "skipped by operator"

    log_path = _write_case_log(
        ctx,
        case["id"],
        f"Manual instructions:\n{instructions}\n\nOperator answer: {answer}\n",
    )
    return _result(case, status, time.perf_counter() - started, detail, log_file=log_path)


def handle_graceful_shutdown(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    config_path = _build_minimal_agent_config(ctx, "graceful_shutdown")
    tasks_dir = ctx.run_dir / "runtime" / "graceful_shutdown" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    command = [
        ctx.python_bin,
        "runner.py",
        "--agent-config",
        str(config_path),
        "--tasks-dir",
        str(tasks_dir),
        "serve",
        "--channel",
        "bluebubbles",
    ]

    result = _spawn_with_sigint(
        command,
        cwd=ctx.repo_root,
        wait_before_sigint_s=float(case.get("wait_before_sigint_s", 4.0)),
        final_timeout_s=int(case.get("final_timeout_s", 30)),
    )
    output = result.output
    log_path = _write_case_log(ctx, case["id"], _command_log_blob(result))

    ok = (
        result.returncode in {0, 130}
        and "Received SIGINT" in output
        and "Server stopped." in output
    )
    detail = (
        "clean shutdown observed"
        if ok
        else "serve did not report expected SIGINT shutdown markers"
    )
    return _result(
        case,
        "pass" if ok else "fail",
        time.perf_counter() - started,
        detail,
        log_file=log_path,
        exit_code=result.returncode,
    )


def handle_poll_backoff(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    config_path = _build_minimal_agent_config(ctx, "poll_backoff")
    command = [
        ctx.python_bin,
        "runner.py",
        "--agent-config",
        str(config_path),
        "listen",
        "--channel",
        "bluebubbles",
    ]

    result = _spawn_with_sigint(
        command,
        cwd=ctx.repo_root,
        wait_before_sigint_s=float(case.get("wait_before_sigint_s", 9.0)),
        final_timeout_s=int(case.get("final_timeout_s", 30)),
    )
    output = result.output
    backoffs = [float(x) for x in re.findall(r"backoff=([0-9]+(?:\.[0-9]+)?)s", output)]
    strictly_increasing = len(backoffs) >= 2 and all(
        later > earlier for earlier, later in zip(backoffs, backoffs[1:])
    )

    ok = (
        "Error polling BlueBubbles" in output
        and strictly_increasing
        and result.returncode in {0, 130}
    )
    detail = (
        f"backoffs={backoffs}"
        if ok
        else f"expected increasing backoff values; observed backoffs={backoffs}"
    )

    log_path = _write_case_log(ctx, case["id"], _command_log_blob(result))
    return _result(
        case,
        "pass" if ok else "fail",
        time.perf_counter() - started,
        detail,
        log_file=log_path,
        exit_code=result.returncode,
    )


def handle_session_ttl(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    from taskrunner.session import SessionManager

    sessions_dir = ctx.run_dir / "runtime" / "session_ttl" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    ttl_hours = float(case.get("ttl_hours", 0.0003))
    sleep_seconds = float(case.get("sleep_seconds", 1.5))

    mgr = SessionManager(sessions_dir=str(sessions_dir), ttl_hours=ttl_hours)
    first = mgr.add_user_message("cli", "smoke ttl")
    first_id = first.session_id
    time.sleep(sleep_seconds)
    second = mgr.get_or_create("cli")
    second_id = second.session_id

    ok = first_id != second_id
    detail = (
        f"session rotated ({first_id} -> {second_id})"
        if ok
        else f"session did not rotate after ttl wait ({first_id})"
    )
    log_path = _write_case_log(
        ctx,
        case["id"],
        (
            f"ttl_hours={ttl_hours}\n"
            f"sleep_seconds={sleep_seconds}\n"
            f"first_session={first_id}\n"
            f"second_session={second_id}\n"
        ),
    )
    return _result(case, "pass" if ok else "fail", time.perf_counter() - started, detail, log_file=log_path)


def handle_broken_secret_validation(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    missing_secret = ctx.run_dir / "runtime" / "missing-secrets.env.enc"
    fake_age_key = ctx.run_dir / "runtime" / "age-key.txt"
    fake_age_key.parent.mkdir(parents=True, exist_ok=True)
    fake_age_key.write_text("AGE-SECRET-KEY-1TESTTESTTESTTESTTESTTESTTESTTESTTESTTEST")

    config_path = _build_minimal_agent_config(
        ctx,
        "broken_secret_validation",
        updates={
            "llm": {
                "secrets": str(missing_secret),
            },
        },
    )

    tasks_dir = ctx.run_dir / "runtime" / "broken_secret_validation" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["AGE_IDENTITY_FILE"] = str(fake_age_key)

    command = [
        ctx.python_bin,
        "runner.py",
        "--agent-config",
        str(config_path),
        "--tasks-dir",
        str(tasks_dir),
        "serve",
        "--channel",
        "bluebubbles",
    ]

    result = _run_command(
        command,
        cwd=ctx.repo_root,
        timeout=int(case.get("timeout", 30)),
        env=env,
    )
    output = result.output
    ok = (
        result.returncode != 0
        and "Startup secrets validation failed" in output
        and "llm.secrets" in output
    )

    log_path = _write_case_log(ctx, case["id"], _command_log_blob(result))
    detail = (
        "startup failed with clear secrets validation error"
        if ok
        else "missing expected startup secrets validation error markers"
    )
    return _result(
        case,
        "pass" if ok else "fail",
        time.perf_counter() - started,
        detail,
        log_file=log_path,
        exit_code=result.returncode,
    )


def handle_no_judge_flag(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    import runner

    with open(ctx.agent_config) as f:
        config = yaml.safe_load(f) or {}

    guardian_cfg = config.setdefault("guardian", {})
    guardian_cfg["enabled"] = True
    llm_judge_cfg = guardian_cfg.setdefault("llm_judge", {})
    llm_judge_cfg["enabled"] = True

    config_dir = ctx.run_dir / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    temp_config = config_dir / "no_judge_flag.yaml"
    temp_config.write_text(yaml.safe_dump(config, sort_keys=False))

    args_without = argparse.Namespace(agent_config=temp_config, no_judge=False)
    args_with = argparse.Namespace(agent_config=temp_config, no_judge=True)

    before = runner._load_agent_def(args_without)
    after = runner._load_agent_def(args_with)

    before_enabled = bool(before.guardian and before.guardian.llm_judge.enabled)
    after_enabled = bool(after.guardian and after.guardian.llm_judge.enabled)

    ok = before_enabled and not after_enabled
    detail = (
        "judge enabled by config, then disabled by --no-judge override"
        if ok
        else f"unexpected judge states before={before_enabled} after={after_enabled}"
    )
    log_path = _write_case_log(
        ctx,
        case["id"],
        f"before_enabled={before_enabled}\nafter_enabled={after_enabled}\nconfig={temp_config}\n",
    )
    return _result(case, "pass" if ok else "fail", time.perf_counter() - started, detail, log_file=log_path)


def handle_audit_cli_runtime(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    from guardian.audit import AuditLogger

    audit_file = ctx.run_dir / "runtime" / "audit_cli" / "guardian_audit.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)

    logger = AuditLogger(audit_file)
    logger.log_screen(input_hash="abc", input_length=12, blocked=True, source="fast_classifier", confidence=0.98)
    logger.log_action(tool_name="send_email", arg_keys=["to"], verdict="review", matched_rule="send_*")
    logger.log_action(tool_name="delete_file", arg_keys=["path"], verdict="deny", matched_rule="delete_*")

    config_path = _build_minimal_agent_config(
        ctx,
        "audit_cli",
        updates={
            "guardian": {
                "enabled": True,
                "fast_classifier": {"enabled": False},
                "llm_judge": {"enabled": False},
                "policy": {"enabled": False},
                "coherence": {"enabled": False},
                "audit": {
                    "enabled": True,
                    "log_file": str(audit_file),
                },
            },
        },
    )

    cmd_tail = [
        ctx.python_bin,
        "runner.py",
        "--agent-config",
        str(config_path),
        "audit",
        "--tail",
        "10",
    ]
    cmd_blocked = [
        ctx.python_bin,
        "runner.py",
        "--agent-config",
        str(config_path),
        "audit",
        "--blocked",
    ]

    tail_result = _run_command(cmd_tail, cwd=ctx.repo_root, timeout=30, env=os.environ.copy())
    blocked_result = _run_command(cmd_blocked, cwd=ctx.repo_root, timeout=30, env=os.environ.copy())

    ok = (
        tail_result.returncode == 0
        and blocked_result.returncode == 0
        and "entries shown" in tail_result.output
        and "No audit entries found." not in blocked_result.output
    )

    blob = (
        "=== audit --tail 10 ===\n"
        f"{_command_log_blob(tail_result)}\n"
        "\n=== audit --blocked ===\n"
        f"{_command_log_blob(blocked_result)}\n"
    )
    log_path = _write_case_log(ctx, case["id"], blob)
    detail = "audit CLI tail + blocked filters returned expected output" if ok else "audit CLI output mismatch"
    exit_code = 0 if ok else max(tail_result.returncode, blocked_result.returncode)
    return _result(case, "pass" if ok else "fail", time.perf_counter() - started, detail, log_file=log_path, exit_code=exit_code)


def handle_onnx_export(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    help_cmd = [ctx.python_bin, "scripts/export-onnx.py", "--help"]
    help_result = _run_command(help_cmd, cwd=ctx.repo_root, timeout=30, env=os.environ.copy())

    if help_result.returncode != 0:
        log_path = _write_case_log(ctx, case["id"], _command_log_blob(help_result))
        return _result(
            case,
            "fail",
            time.perf_counter() - started,
            "export-onnx.py --help failed",
            log_file=log_path,
            exit_code=help_result.returncode,
        )

    if not ctx.args.live_llm:
        log_path = _write_case_log(
            ctx,
            case["id"],
            (
                "Live export skipped (requires --live-llm).\n\n"
                "Help command succeeded.\n\n"
                + _command_log_blob(help_result)
            ),
        )
        return _result(
            case,
            "skip",
            time.perf_counter() - started,
            "help succeeded; live model export skipped (use --live-llm)",
            log_file=log_path,
            exit_code=help_result.returncode,
        )

    out_dir = ctx.run_dir / "runtime" / "onnx-export"
    export_cmd = [
        ctx.python_bin,
        "scripts/export-onnx.py",
        ctx.args.onnx_model,
        "--output-dir",
        str(out_dir),
    ]
    if bool(case.get("validate", False)):
        export_cmd.append("--validate")

    export_result = _run_command(
        export_cmd,
        cwd=ctx.repo_root,
        timeout=int(case.get("timeout", 1800)),
        env=os.environ.copy(),
    )
    onnx_files = sorted(out_dir.glob("*.onnx")) if out_dir.exists() else []
    ok = export_result.returncode == 0 and len(onnx_files) > 0

    blob = (
        "=== export-onnx --help ===\n"
        f"{_command_log_blob(help_result)}\n"
        "\n=== export-onnx live ===\n"
        f"{_command_log_blob(export_result)}\n"
        f"\nONNX files: {[str(p) for p in onnx_files]}\n"
    )
    log_path = _write_case_log(ctx, case["id"], blob)
    detail = (
        f"exported {len(onnx_files)} ONNX artifact(s)"
        if ok
        else "live ONNX export failed or no .onnx artifact found"
    )
    return _result(
        case,
        "pass" if ok else "fail",
        time.perf_counter() - started,
        detail,
        log_file=log_path,
        exit_code=export_result.returncode,
    )


def handle_quick_chat(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    container_mode = bool(case.get("container_mode", False))
    command = [
        ctx.python_bin,
        "runner.py",
        "--agent-config",
        str(ctx.agent_config),
    ]
    if container_mode:
        command.append("--containers")
    command.extend(["chat", "--simple"])

    messages = [str(x) for x in case.get("messages", [])]
    result = _spawn_with_sigint(
        command,
        cwd=ctx.repo_root,
        wait_before_sigint_s=float(case.get("wait_before_sigint_s", 3.0)),
        final_timeout_s=int(case.get("final_timeout_s", 60)),
        stdin_lines=messages,
        line_delay_s=float(case.get("line_delay_s", 2.0)),
    )
    output = result.output
    assistant_count = len(re.findall(r"Assistant:", output))

    ok = (
        result.returncode in {0, 130}
        and assistant_count >= int(case.get("min_assistant_lines", 2))
        and "Session cleared." in output
        and ("Goodbye!" in output or "Chat stopped." in output)
    )

    detail = (
        f"assistant_lines={assistant_count}"
        if ok
        else f"quick chat assertions failed (assistant_lines={assistant_count})"
    )
    log_path = _write_case_log(ctx, case["id"], _command_log_blob(result))
    return _result(
        case,
        "pass" if ok else "fail",
        time.perf_counter() - started,
        detail,
        log_file=log_path,
        exit_code=result.returncode,
    )


def handle_container_simple_task(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    tasks_dir = ctx.run_dir / "runtime" / "container_simple_task" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_name = str(case.get("task_name", "container_weather_smoke"))
    verify_token = str(case.get("verify_token", "SIMPLE_CONTAINER_OK"))
    location = str(case.get("location", "denver"))

    task_path = tasks_dir / f"{task_name}.yaml"
    task_path.write_text(
        yaml.safe_dump(
            {
                "name": task_name,
                "schedule": "* * * * *",
                "executors": {
                    "weather": {
                        "args": {
                            "location": location,
                        },
                    },
                },
                "prompt": (
                    "Weather data:\n\n"
                    "{weather}\n\n"
                    f"Reply in one concise sentence and include token {verify_token}."
                ),
                "output": {
                    "type": "stdout",
                    "to": "",
                },
                "llm": {
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 180,
                },
            },
            sort_keys=False,
        )
    )

    command = [
        ctx.python_bin,
        "runner.py",
        "-v",
        "--containers",
        "--tasks-dir",
        str(tasks_dir),
        "run",
        task_name,
    ]
    result = _run_command(
        command,
        cwd=ctx.repo_root,
        timeout=int(case.get("timeout", 900)),
        env=os.environ.copy(),
    )
    output = result.output
    ok = (
        result.returncode == 0
        and f"Task '{task_name}' completed." in output
        and verify_token in output
    )
    detail = (
        f"container simple task succeeded with token {verify_token}"
        if ok
        else "container simple task did not produce expected completion/token output"
    )
    log_path = _write_case_log(ctx, case["id"], _command_log_blob(result))
    return _result(
        case,
        "pass" if ok else "fail",
        time.perf_counter() - started,
        detail,
        log_file=log_path,
        exit_code=result.returncode,
    )


def handle_container_agent_tool_call(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    token = str(case.get("verify_token", "AGENT_TOOL_CONTAINER_OK"))
    attempts = int(case.get("attempts", 2))
    message_template = str(
        case.get(
            "message",
            (
                "Call the check_weather tool with location Denver. "
                "You must call the tool before answering. "
                f"End your final response with {token}."
            ),
        )
    )

    config_path = _build_minimal_agent_config(
        ctx,
        "container_agent_tool_call",
        updates={
            "tools": {
                "check_weather": {
                    "executor": "weather",
                    "description": "Get current weather and forecast",
                    "parameters": {
                        "location": {
                            "type": "string",
                            "description": "City name or coordinates",
                            "required": True,
                        },
                    },
                },
            },
            "llm": {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 220,
            },
            "guardian": {
                "enabled": False,
            },
        },
    )

    logs: list[str] = []
    last_rc = 1
    for attempt in range(1, attempts + 1):
        command = [
            ctx.python_bin,
            "runner.py",
            "-v",
            "--containers",
            "--agent-config",
            str(config_path),
            "chat",
            "--simple",
            "--new",
        ]
        lines = [message_template, "/clear"]
        result = _spawn_with_sigint(
            command,
            cwd=ctx.repo_root,
            wait_before_sigint_s=float(case.get("wait_before_sigint_s", 3.0)),
            final_timeout_s=int(case.get("final_timeout_s", 90)),
            stdin_lines=lines,
            line_delay_s=float(case.get("line_delay_s", 2.0)),
            env=os.environ.copy(),
        )
        last_rc = result.returncode
        output = result.output
        tool_marker_seen = bool(re.search(r"Executing tool check_weather", output))
        token_seen = token in output
        logs.append(
            (
                f"=== Attempt {attempt} ===\n"
                f"tool_marker_seen={tool_marker_seen}\n"
                f"token_seen={token_seen}\n"
                f"{_command_log_blob(result)}\n"
            )
        )

        if result.returncode in {0, 130} and tool_marker_seen and token_seen:
            log_path = _write_case_log(ctx, case["id"], "\n".join(logs))
            return _result(
                case,
                "pass",
                time.perf_counter() - started,
                f"agent container path exercised with tool call (attempt {attempt})",
                log_file=log_path,
                exit_code=result.returncode,
            )

    log_path = _write_case_log(ctx, case["id"], "\n".join(logs))
    return _result(
        case,
        "fail",
        time.perf_counter() - started,
        "did not observe both tool execution marker and response token in container chat",
        log_file=log_path,
        exit_code=last_rc,
    )


def handle_cli_help(case: dict[str, Any], ctx: SmokeContext) -> CaseResult:
    started = time.perf_counter()
    commands = [
        [ctx.python_bin, "runner.py", "--help"],
        [ctx.python_bin, "runner.py", "chat", "--help"],
        [ctx.python_bin, "runner.py", "listen", "--help"],
        [ctx.python_bin, "runner.py", "serve", "--help"],
        [ctx.python_bin, "runner.py", "audit", "--help"],
        [ctx.python_bin, "runner.py", "run", "--help"],
    ]

    failures: list[str] = []
    blobs: list[str] = []
    final_code = 0

    for cmd in commands:
        result = _run_command(cmd, cwd=ctx.repo_root, timeout=20, env=os.environ.copy())
        final_code = max(final_code, result.returncode)
        blobs.append(_command_log_blob(result))
        if result.returncode != 0:
            failures.append(f"non-zero exit: {' '.join(cmd)} -> {result.returncode}")
        elif "usage:" not in result.output.lower():
            failures.append(f"missing usage output: {' '.join(cmd)}")

    log_path = _write_case_log(ctx, case["id"], "\n\n".join(blobs))
    if failures:
        return _result(
            case,
            "fail",
            time.perf_counter() - started,
            "; ".join(failures),
            log_file=log_path,
            exit_code=final_code,
        )

    return _result(
        case,
        "pass",
        time.perf_counter() - started,
        "runner.py help surfaces available",
        log_file=log_path,
        exit_code=0,
    )


HANDLERS: dict[str, Any] = {
    "command": handle_command,
    "manual": handle_manual,
    "graceful_shutdown": handle_graceful_shutdown,
    "poll_backoff": handle_poll_backoff,
    "session_ttl": handle_session_ttl,
    "broken_secret_validation": handle_broken_secret_validation,
    "no_judge_flag": handle_no_judge_flag,
    "audit_cli_runtime": handle_audit_cli_runtime,
    "onnx_export": handle_onnx_export,
    "quick_chat": handle_quick_chat,
    "container_simple_task": handle_container_simple_task,
    "container_agent_tool_call": handle_container_agent_tool_call,
    "cli_help": handle_cli_help,
}


def _load_cases(path: Path) -> list[dict[str, Any]]:
    if yaml is None:
        raise RuntimeError(
            "Missing dependency: pyyaml. Install project deps first "
            "(example: uv pip install -e \".[dev]\")."
        )
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    version = raw.get("version")
    if version != 1:
        raise ValueError(f"Unsupported smoke case file version: {version!r}")

    cases = raw.get("cases")
    if not isinstance(cases, list):
        raise ValueError("smoke cases file must contain a list under 'cases'")

    for case in cases:
        for required in ("id", "phase", "title", "handler"):
            if required not in case:
                raise ValueError(f"Case missing {required!r}: {case}")
    return cases


def _detect_docker() -> tuple[bool, str]:
    result = _run_command(
        ["docker", "version"],
        cwd=ROOT,
        timeout=10,
        env=os.environ.copy(),
    )
    if result.returncode == 0:
        return True, "docker available"
    msg = (result.stderr or result.stdout or "docker unavailable").strip()
    return False, msg.splitlines()[-1] if msg else "docker unavailable"


def _should_include_case(case: dict[str, Any], args: argparse.Namespace) -> bool:
    tags = set(case.get("tags", []))
    phase = case["phase"]

    if args.case_ids:
        return case["id"] in args.case_ids

    if args.quick:
        return "quick" in tags

    if args.phase == "foundation":
        return phase == "foundation"
    if args.phase == "security":
        return phase == "security"
    if args.phase == "extras":
        return phase == "extras"

    # phase == all
    if phase == "foundation" or phase == "security":
        return True
    if phase == "quick":
        return True
    if phase == "extras":
        return bool(args.include_extras)
    if phase == "containers":
        return bool(args.containers)
    return False


def _apply_runtime_gates(case: dict[str, Any], ctx: SmokeContext) -> CaseResult | None:
    if case.get("requires_live_llm") and not ctx.args.live_llm:
        return _skip_result(case, "requires --live-llm")
    if case.get("requires_live_docker"):
        if not ctx.args.live_docker:
            return _skip_result(case, "requires --live-docker")
        if not ctx.docker_available:
            return _skip_result(case, f"docker unavailable: {ctx.docker_reason}")
    if case.get("container_mode") and not ctx.args.containers:
        return _skip_result(case, "requires --containers")
    return None


def run_cases(cases: list[dict[str, Any]], ctx: SmokeContext) -> list[CaseResult]:
    selected = [case for case in cases if _should_include_case(case, ctx.args)]
    selected.sort(key=lambda c: c.get("order", 10_000))

    if not selected:
        print("No smoke cases selected.")
        return []

    print(f"Run directory: {ctx.run_dir}")
    print(f"Selected cases: {len(selected)}")
    print()

    results: list[CaseResult] = []
    for idx, case in enumerate(selected, start=1):
        print(f"[{idx:02d}/{len(selected):02d}] {case['id']} :: {case['title']}")

        gate_result = _apply_runtime_gates(case, ctx)
        if gate_result is not None:
            results.append(gate_result)
            print(f"  -> {gate_result.status.upper()}: {gate_result.detail}")
            continue

        handler_name = case["handler"]
        handler = HANDLERS.get(handler_name)
        if handler is None:
            res = _result(case, "fail", 0.0, f"unknown handler: {handler_name}")
            results.append(res)
            print(f"  -> FAIL: {res.detail}")
            continue

        try:
            res = handler(case, ctx)
        except Exception as exc:  # noqa: BLE001
            log_path = _write_case_log(
                ctx,
                case["id"],
                f"Unhandled exception in case {case['id']}:\n{exc}\n",
            )
            res = _result(
                case,
                "fail",
                0.0,
                f"unhandled exception: {exc}",
                log_file=log_path,
            )

        results.append(res)
        print(f"  -> {res.status.upper()}: {res.detail}")

    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Creel smoke checks.")
    parser.add_argument("--cases-file", type=Path, default=DEFAULT_CASES_FILE, help="Path to smoke_cases.yaml")
    parser.add_argument(
        "--phase",
        choices=["foundation", "security", "extras", "all"],
        default="all",
        help="Phase selection (default: all)",
    )
    parser.add_argument("--quick", action="store_true", help="Run only quick critical-path checks")
    parser.add_argument("--containers", action="store_true", help="Include container-mode smoke checks")
    parser.add_argument("--live-llm", action="store_true", help="Enable checks that call live LLM/model APIs")
    parser.add_argument("--live-docker", action="store_true", help="Enable Docker build/run checks")
    parser.add_argument("--manual-ok", action="store_true", help="Auto-pass manual checkpoints")
    parser.add_argument(
        "--agent-config",
        type=Path,
        default=ROOT / "agent.yaml",
        help="Agent config for live chat checks (default: ./agent.yaml)",
    )
    parser.add_argument(
        "--onnx-model",
        type=str,
        default=DEFAULT_ONNX_MODEL,
        help=f"Model for live ONNX export (default: {DEFAULT_ONNX_MODEL})",
    )
    parser.add_argument("--timeout", type=int, default=900, help="Default timeout for command checks (seconds)")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR, help="Directory for smoke run artifacts")
    parser.add_argument("--no-extras", action="store_true", help="Exclude extra coverage checks")
    parser.add_argument("--list-cases", action="store_true", help="List available cases and exit")
    parser.add_argument(
        "--case",
        dest="case_ids",
        action="append",
        default=[],
        help="Run only specific case ID(s); can be repeated",
    )
    return parser.parse_args()


def main() -> int:
    if yaml is None:
        print(
            "Error: missing dependency 'pyyaml'. Install project dependencies first "
            "(example: uv pip install -e \".[dev]\").",
            file=sys.stderr,
        )
        return 2

    args = _parse_args()
    args.include_extras = not args.no_extras

    cases = _load_cases(args.cases_file)
    if args.list_cases:
        for case in sorted(cases, key=lambda c: c.get("order", 10_000)):
            print(f"{case['id']:<40} [{case['phase']}] {case['title']}")
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = args.runs_dir / run_id
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    docker_available, docker_reason = _detect_docker() if (args.live_docker or args.containers) else (False, "not checked")
    ctx = SmokeContext(
        args=args,
        repo_root=ROOT,
        runs_dir=args.runs_dir,
        run_dir=run_dir,
        logs_dir=logs_dir,
        python_bin=sys.executable,
        agent_config=args.agent_config,
        docker_available=docker_available,
        docker_reason=docker_reason,
    )

    started_at = _now_iso()
    results = run_cases(cases, ctx)
    finished_at = _now_iso()

    counts = {
        "pass": sum(1 for r in results if r.status == "pass"),
        "fail": sum(1 for r in results if r.status == "fail"),
        "skip": sum(1 for r in results if r.status == "skip"),
        "total": len(results),
    }

    summary = {
        "started_at": started_at,
        "finished_at": finished_at,
        "run_dir": str(run_dir),
        "args": {
            **{k: v for k, v in vars(args).items() if k not in {"runs_dir", "cases_file", "agent_config"}},
            "runs_dir": str(args.runs_dir),
            "cases_file": str(args.cases_file),
            "agent_config": str(args.agent_config),
        },
        "docker": {
            "checked": bool(args.live_docker or args.containers),
            "available": docker_available,
            "reason": docker_reason,
        },
        "counts": counts,
        "results": [asdict(r) for r in results],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    print()
    print("Summary:")
    print(f"  pass: {counts['pass']}")
    print(f"  fail: {counts['fail']}")
    print(f"  skip: {counts['skip']}")
    print(f"  total: {counts['total']}")
    print(f"  artifacts: {run_dir}")

    return 1 if counts["fail"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
