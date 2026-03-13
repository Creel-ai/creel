"""Diagnostic command that checks Creel installation health.

Validates config, tests connections, verifies executors, and reports issues.
Each check shows a pass/warning/error status with colorized output.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from creel import paths

logger = logging.getLogger(__name__)

# Status symbols and colors (ANSI)
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

PASS = f"{_GREEN}\u2713{_RESET}"
WARN = f"{_YELLOW}\u26a0{_RESET}"
FAIL = f"{_RED}\u2717{_RESET}"


@dataclass
class CheckResult:
    """Result from a single health check."""

    status: str  # "pass", "warn", "error"
    label: str
    message: str
    fixable: bool = False
    fix_label: str = ""

    @property
    def icon(self) -> str:
        return {
            "pass": PASS,
            "warn": WARN,
            "error": FAIL,
        }.get(self.status, WARN)


@dataclass
class DoctorReport:
    """Aggregate report from all health checks."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == "pass")

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    @property
    def errors(self) -> int:
        return sum(1 for c in self.checks if c.status == "error")

    @property
    def fixable(self) -> list[CheckResult]:
        return [c for c in self.checks if c.fixable and c.status != "pass"]

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)


def _section_header(title: str) -> str:
    return f"\n{_BOLD}{title}{_RESET}"


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def check_config(agent_config_path: Path | None = None) -> list[CheckResult]:
    """Validate agent.yaml configuration."""
    results: list[CheckResult] = []

    config_path = agent_config_path or paths.agent_config()
    if not config_path.exists():
        results.append(
            CheckResult(
                status="error",
                label="Agent config",
                message=f"Not found: {config_path}",
                fixable=True,
                fix_label="Run 'creel init' to create default config",
            )
        )
        return results

    try:
        from creel.models import load_agent_config

        config = load_agent_config(config_path)
        results.append(
            CheckResult(
                status="pass",
                label="Agent config",
                message=f"Valid ({config_path})",
            )
        )

        # Check sub-configs
        if config.tools:
            results.append(
                CheckResult(
                    status="pass",
                    label="Tools defined",
                    message=f"{len(config.tools)} tool(s) configured",
                )
            )
        else:
            results.append(
                CheckResult(
                    status="warn",
                    label="Tools defined",
                    message="No tools configured in agent.yaml",
                )
            )

    except Exception as exc:
        results.append(
            CheckResult(
                status="error",
                label="Agent config",
                message=f"Parse error: {exc}",
            )
        )
    return results


def check_llm_provider(agent_config_path: Path | None = None) -> list[CheckResult]:
    """Test LLM provider connectivity."""
    results: list[CheckResult] = []

    # Check for API key
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        # Try loading from encrypted secrets
        try:
            config_path = agent_config_path or paths.agent_config()
            if config_path.exists():
                from creel.models import load_agent_config

                config = load_agent_config(config_path)
                if config.llm.secrets:
                    secrets_path = paths.secrets_dir() / config.llm.secrets
                    if secrets_path.exists():
                        from creel.secrets import decrypt_env_file

                        env = decrypt_env_file(secrets_path)
                        api_key = env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")
        except Exception:
            pass

    if not api_key:
        results.append(
            CheckResult(
                status="error",
                label="LLM API key",
                message="No ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN found",
                fixable=True,
                fix_label="Set ANTHROPIC_API_KEY env var or configure llm.secrets in agent.yaml",
            )
        )
        return results

    # Validate key
    from creel.validation import validate_anthropic_key

    vr = validate_anthropic_key(api_key)
    if vr.ok:
        results.append(
            CheckResult(
                status="pass",
                label="LLM provider",
                message=f"Anthropic API: {vr.message}",
            )
        )
    else:
        results.append(
            CheckResult(
                status="error",
                label="LLM provider",
                message=f"Anthropic API: {vr.message}",
            )
        )

    return results


def check_executors(agent_config_path: Path | None = None) -> list[CheckResult]:
    """Check executor health — verify executor packages are importable."""
    results: list[CheckResult] = []

    config_path = agent_config_path or paths.agent_config()
    if not config_path.exists():
        results.append(
            CheckResult(
                status="warn",
                label="Executors",
                message="Skipped (no agent config)",
            )
        )
        return results

    try:
        from creel.models import load_agent_config

        config = load_agent_config(config_path)
    except Exception as exc:
        results.append(
            CheckResult(
                status="warn",
                label="Executors",
                message=f"Skipped (config error: {exc})",
            )
        )
        return results

    if not config.tools:
        results.append(
            CheckResult(
                status="warn",
                label="Executors",
                message="No tools configured",
            )
        )
        return results

    # Collect unique executor names
    executors = {tc.executor for tc in config.tools.values()}

    for executor_name in sorted(executors):
        module_name = f"executors.{executor_name}"
        try:
            importlib.import_module(module_name)
            results.append(
                CheckResult(
                    status="pass",
                    label=f"Executor: {executor_name}",
                    message="Module importable",
                )
            )
        except ImportError:
            results.append(
                CheckResult(
                    status="warn",
                    label=f"Executor: {executor_name}",
                    message=f"Cannot import {module_name} (may require container mode)",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    status="error",
                    label=f"Executor: {executor_name}",
                    message=f"Import error: {exc}",
                )
            )

    # Check Docker availability for container mode
    docker = shutil.which("docker")
    if docker:
        try:
            proc = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10,
            )
            if proc.returncode == 0:
                results.append(
                    CheckResult(
                        status="pass",
                        label="Docker",
                        message="Available for container-mode executors",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        status="warn",
                        label="Docker",
                        message="Installed but not running (container mode unavailable)",
                    )
                )
        except (subprocess.TimeoutExpired, OSError):
            results.append(
                CheckResult(
                    status="warn",
                    label="Docker",
                    message="Installed but not responsive",
                )
            )
    else:
        results.append(
            CheckResult(
                status="warn",
                label="Docker",
                message="Not installed (container-mode executors unavailable)",
            )
        )

    return results


def check_channels(agent_config_path: Path | None = None) -> list[CheckResult]:
    """Check channel connectivity."""
    results: list[CheckResult] = []

    config_path = agent_config_path or paths.agent_config()
    if not config_path.exists():
        results.append(
            CheckResult(
                status="warn",
                label="Channels",
                message="Skipped (no agent config)",
            )
        )
        return results

    try:
        from creel.models import load_agent_config

        config = load_agent_config(config_path)
    except Exception as exc:
        results.append(
            CheckResult(
                status="warn",
                label="Channels",
                message=f"Skipped (config error: {exc})",
            )
        )
        return results

    configured = config.channels.configured_channels()
    if not configured:
        results.append(
            CheckResult(
                status="warn",
                label="Channels",
                message="No channels configured",
            )
        )
        return results

    for ch_name in configured:
        ch_config = config.channels.get_channel_config(ch_name)
        if ch_config is None:
            results.append(
                CheckResult(
                    status="warn",
                    label=f"Channel: {ch_name}",
                    message="Config missing",
                )
            )
            continue

        # Channel-specific connectivity checks
        if ch_name == "telegram":
            bot_token = ch_config.get("bot_token", "")
            expanded = os.path.expandvars(bot_token)
            if expanded.startswith("$") or not expanded:
                results.append(
                    CheckResult(
                        status="error",
                        label="Channel: telegram",
                        message="Bot token not set (env var not resolved)",
                        fixable=True,
                        fix_label="Set TELEGRAM_BOT_TOKEN environment variable",
                    )
                )
            else:
                from creel.validation import validate_telegram_token

                vr = validate_telegram_token(expanded)
                status = "pass" if vr.ok else "error"
                results.append(
                    CheckResult(
                        status=status,
                        label="Channel: telegram",
                        message=vr.message,
                    )
                )
        elif ch_name == "imessage":
            # iMessage works via local bridge — just check config exists
            results.append(
                CheckResult(
                    status="pass",
                    label="Channel: imessage",
                    message=f"Configured (listen_to: {ch_config.get('listen_to', '?')})",
                )
            )
        elif ch_name == "bluebubbles":
            server_url = ch_config.get("server_url", "")
            if server_url:
                results.append(
                    CheckResult(
                        status="pass",
                        label="Channel: bluebubbles",
                        message=f"Configured (server: {server_url})",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        status="error",
                        label="Channel: bluebubbles",
                        message="server_url not set",
                    )
                )
        else:
            results.append(
                CheckResult(
                    status="pass",
                    label=f"Channel: {ch_name}",
                    message="Configured",
                )
            )

    return results


def check_guardian(agent_config_path: Path | None = None) -> list[CheckResult]:
    """Validate Guardian security pipeline."""
    results: list[CheckResult] = []

    config_path = agent_config_path or paths.agent_config()
    if not config_path.exists():
        results.append(
            CheckResult(
                status="warn",
                label="Guardian",
                message="Skipped (no agent config)",
            )
        )
        return results

    try:
        from creel.models import load_agent_config

        config = load_agent_config(config_path)
    except Exception as exc:
        results.append(
            CheckResult(
                status="warn",
                label="Guardian",
                message=f"Skipped (config error: {exc})",
            )
        )
        return results

    if config.guardian is None or not config.guardian.enabled:
        results.append(
            CheckResult(
                status="warn",
                label="Guardian",
                message="Disabled — no security pipeline active",
            )
        )
        return results

    gc = config.guardian

    # Policy engine
    if gc.policy.enabled:
        policy_file = gc.policy.policy_file or str(paths.policies_dir() / "default.yaml")
        if Path(policy_file).exists():
            results.append(
                CheckResult(
                    status="pass",
                    label="Guardian: policy engine",
                    message=f"Enabled ({policy_file})",
                )
            )
        else:
            results.append(
                CheckResult(
                    status="error",
                    label="Guardian: policy engine",
                    message=f"Policy file missing: {policy_file}",
                    fixable=True,
                    fix_label="Run 'creel init' to create default policy",
                )
            )
    else:
        results.append(
            CheckResult(
                status="warn",
                label="Guardian: policy engine",
                message="Disabled",
            )
        )

    # Fast classifier
    if gc.fast_classifier.enabled:
        try:
            from guardian.fast_classifier import FastClassifier

            _fc = FastClassifier(gc.fast_classifier)
            results.append(
                CheckResult(
                    status="pass",
                    label="Guardian: fast classifier",
                    message="Enabled (DeBERTa/ONNX)",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    status="warn",
                    label="Guardian: fast classifier",
                    message=f"Cannot initialize: {exc}",
                )
            )
    else:
        results.append(
            CheckResult(
                status="warn",
                label="Guardian: fast classifier",
                message="Disabled",
            )
        )

    # LLM judge
    if gc.llm_judge.enabled:
        results.append(
            CheckResult(
                status="pass",
                label="Guardian: LLM judge",
                message=f"Enabled (model: {gc.llm_judge.model})",
            )
        )
    else:
        results.append(
            CheckResult(
                status="warn",
                label="Guardian: LLM judge",
                message="Disabled",
            )
        )

    # Audit logging
    if gc.audit.enabled:
        audit_file = gc.audit.log_file or str(paths.audit_log())
        results.append(
            CheckResult(
                status="pass",
                label="Guardian: audit log",
                message=f"Enabled ({audit_file})",
            )
        )
    else:
        results.append(
            CheckResult(
                status="warn",
                label="Guardian: audit log",
                message="Disabled — no audit trail",
            )
        )

    # Drift detection
    if gc.drift.enabled:
        results.append(
            CheckResult(
                status="pass",
                label="Guardian: drift detection",
                message=f"Enabled (z_threshold={gc.drift.z_threshold})",
            )
        )
    else:
        results.append(
            CheckResult(
                status="warn",
                label="Guardian: drift detection",
                message="Disabled",
            )
        )

    return results


def check_security_posture(agent_config_path: Path | None = None) -> list[CheckResult]:
    """Check overall security posture."""
    results: list[CheckResult] = []

    # Age identity for secrets decryption
    age_key = Path(os.environ.get("AGE_IDENTITY_FILE", str(Path.home() / ".age" / "key.txt")))
    if age_key.exists():
        results.append(
            CheckResult(
                status="pass",
                label="Age identity",
                message=f"Found ({age_key})",
            )
        )
        # Check permissions
        mode = age_key.stat().st_mode & 0o777
        if mode > 0o600:
            results.append(
                CheckResult(
                    status="warn",
                    label="Age key permissions",
                    message=f"Permissions too open ({oct(mode)}), recommend 0600",
                    fixable=True,
                    fix_label=f"chmod 600 {age_key}",
                )
            )
        else:
            results.append(
                CheckResult(
                    status="pass",
                    label="Age key permissions",
                    message=f"Secure ({oct(mode)})",
                )
            )
    else:
        results.append(
            CheckResult(
                status="warn",
                label="Age identity",
                message="Not found (encrypted secrets unavailable)",
            )
        )

    # Check encrypted secrets exist
    secrets = paths.secrets_dir()
    if secrets.is_dir():
        enc_files = list(secrets.glob("*.env.enc"))
        if enc_files:
            results.append(
                CheckResult(
                    status="pass",
                    label="Encrypted secrets",
                    message=f"{len(enc_files)} encrypted file(s) in {secrets}",
                )
            )
        else:
            results.append(
                CheckResult(
                    status="warn",
                    label="Encrypted secrets",
                    message="No .env.enc files found in secrets directory",
                )
            )
    else:
        results.append(
            CheckResult(
                status="warn",
                label="Encrypted secrets",
                message=f"Secrets directory not found: {secrets}",
            )
        )

    # Check for plaintext .env files in home dir (security risk)
    creel_home = paths.creel_home()
    if creel_home.is_dir():
        plain_envs = list(creel_home.glob("*.env"))
        if plain_envs:
            results.append(
                CheckResult(
                    status="warn",
                    label="Plaintext secrets",
                    message=(
                        f"{len(plain_envs)} plaintext .env file(s) found — "
                        "consider encrypting with 'creel encrypt'"
                    ),
                    fixable=True,
                    fix_label="Run 'creel encrypt <file>' for each .env file",
                )
            )

    return results


def check_sessions() -> list[CheckResult]:
    """Check session store health."""
    results: list[CheckResult] = []

    sessions = paths.sessions_dir()
    if not sessions.is_dir():
        results.append(
            CheckResult(
                status="warn",
                label="Session store",
                message=f"Directory not found: {sessions}",
                fixable=True,
                fix_label="Will be created on first session",
            )
        )
        return results

    session_files = list(sessions.glob("*.json"))
    # Exclude index files
    session_files = [f for f in session_files if not f.name.startswith("_")]

    if not session_files:
        results.append(
            CheckResult(
                status="pass",
                label="Session store",
                message="Empty (no active sessions)",
            )
        )
        return results

    # Count and check for stale sessions
    stale_count = 0
    corrupt_count = 0
    now = time.time()
    stale_threshold = 7 * 24 * 3600  # 7 days

    for sf in session_files:
        try:
            data = json.loads(sf.read_text())
            last_active = data.get("last_active", 0)
            if now - last_active > stale_threshold:
                stale_count += 1
        except (json.JSONDecodeError, OSError):
            corrupt_count += 1

    results.append(
        CheckResult(
            status="pass",
            label="Session store",
            message=f"{len(session_files)} session(s) in {sessions}",
        )
    )

    if stale_count > 0:
        results.append(
            CheckResult(
                status="warn",
                label="Stale sessions",
                message=f"{stale_count} session(s) inactive for >7 days",
                fixable=True,
                fix_label="Clean up old session files",
            )
        )

    if corrupt_count > 0:
        results.append(
            CheckResult(
                status="error",
                label="Corrupt sessions",
                message=f"{corrupt_count} session file(s) could not be read",
                fixable=True,
                fix_label="Remove corrupt session files",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Fix mode
# ---------------------------------------------------------------------------


def apply_fixes(report: DoctorReport) -> list[str]:
    """Apply auto-fixable remediation actions. Returns list of actions taken."""
    actions: list[str] = []

    for check in report.fixable:
        if check.label == "Age key permissions":
            age_key = Path(
                os.environ.get("AGE_IDENTITY_FILE", str(Path.home() / ".age" / "key.txt"))
            )
            if age_key.exists():
                age_key.chmod(0o600)
                actions.append(f"Fixed permissions on {age_key} to 0600")

        elif check.label == "Stale sessions":
            sessions = paths.sessions_dir()
            if sessions.is_dir():
                now = time.time()
                stale_threshold = 7 * 24 * 3600
                cleaned = 0
                for sf in sessions.glob("*.json"):
                    if sf.name.startswith("_"):
                        continue
                    try:
                        data = json.loads(sf.read_text())
                        last_active = data.get("last_active", 0)
                        if now - last_active > stale_threshold:
                            sf.unlink()
                            cleaned += 1
                    except (json.JSONDecodeError, OSError):
                        pass
                if cleaned:
                    actions.append(f"Cleaned {cleaned} stale session file(s)")

        elif check.label == "Corrupt sessions":
            sessions = paths.sessions_dir()
            if sessions.is_dir():
                cleaned = 0
                for sf in sessions.glob("*.json"):
                    if sf.name.startswith("_"):
                        continue
                    try:
                        json.loads(sf.read_text())
                    except (json.JSONDecodeError, OSError):
                        sf.unlink()
                        cleaned += 1
                if cleaned:
                    actions.append(f"Removed {cleaned} corrupt session file(s)")

        elif check.label == "Session store" and "not found" in check.message:
            sessions = paths.sessions_dir()
            sessions.mkdir(parents=True, exist_ok=True)
            actions.append(f"Created session directory: {sessions}")

    return actions


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_doctor(
    agent_config_path: Path | None = None,
    fix: bool = False,
    no_color: bool = False,
) -> DoctorReport:
    """Run all health checks and optionally apply fixes."""
    report = DoctorReport()

    config_sections: list[tuple[str, list[CheckResult]]] = [
        ("Configuration", check_config(agent_config_path)),
        ("LLM Provider", check_llm_provider(agent_config_path)),
        ("Executors", check_executors(agent_config_path)),
        ("Channels", check_channels(agent_config_path)),
        ("Guardian Pipeline", check_guardian(agent_config_path)),
        ("Security Posture", check_security_posture(agent_config_path)),
        ("Session Store", check_sessions()),
    ]

    for title, results in config_sections:
        print(_section_header(title) if not no_color else f"\n{title}")
        for r in results:
            report.add(r)
            icon = r.icon if not no_color else {"pass": "OK", "warn": "!!", "error": "XX"}[r.status]
            print(f"  {icon} {r.label}: {r.message}")

    # Summary
    print(f"\n{_BOLD}Summary{_RESET}" if not no_color else "\nSummary")
    print(f"  {report.passed} passed, {report.warnings} warning(s), {report.errors} error(s)")

    if fix and report.fixable:
        print(f"\n{_BOLD}Applying fixes...{_RESET}" if not no_color else "\nApplying fixes...")
        actions = apply_fixes(report)
        if actions:
            for action in actions:
                print(f"  {PASS if not no_color else 'OK'} {action}")
        else:
            print(
                f"  {_DIM}No automatic fixes available{_RESET}"
                if not no_color
                else "  No automatic fixes available"
            )
    elif report.fixable and not fix:
        print(
            f"\n{_DIM}Run 'creel doctor --fix' to auto-fix {len(report.fixable)} issue(s){_RESET}"
            if not no_color
            else f"\nRun 'creel doctor --fix' to auto-fix {len(report.fixable)} issue(s)"
        )

    return report
