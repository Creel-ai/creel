"""Tests for creel.doctor — installation health check diagnostics."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from creel.doctor import (
    FIX_AGE_KEY_PERMISSIONS,
    FIX_CORRUPT_SESSIONS,
    FIX_SESSION_DIR_MISSING,
    FIX_STALE_SESSIONS,
    CheckResult,
    DoctorReport,
    apply_fixes,
    check_channels,
    check_config,
    check_executors,
    check_guardian,
    check_llm_provider,
    check_security_posture,
    check_sessions,
    run_doctor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def creel_home(tmp_path):
    """Set up a minimal CREEL_HOME with agent.yaml."""
    home = tmp_path / ".creel"
    home.mkdir()
    (home / "sessions").mkdir()
    (home / "secrets").mkdir()
    (home / "policies").mkdir()

    agent_yaml = home / "agent.yaml"
    agent_yaml.write_text(
        """\
system_prompt: "You are a helpful assistant."
tools:
  weather:
    executor: weather
    description: "Get weather data"
llm:
  model: claude-sonnet-4-20250514
  max_tokens: 300
channels: {}
"""
    )

    # Write a minimal policy file
    policy = home / "policies" / "default.yaml"
    policy.write_text(
        """\
allow:
  - weather
review:
  - send_email
deny:
  - delete_*
"""
    )

    with patch.dict(os.environ, {"CREEL_HOME": str(home)}):
        yield home


@pytest.fixture()
def agent_config_path(creel_home):
    return creel_home / "agent.yaml"


# ---------------------------------------------------------------------------
# CheckResult / DoctorReport
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_pass_icon(self):
        r = CheckResult(status="pass", label="test", message="ok")
        assert "\u2713" in r.icon

    def test_warn_icon(self):
        r = CheckResult(status="warn", label="test", message="warning")
        assert "\u26a0" in r.icon

    def test_error_icon(self):
        r = CheckResult(status="error", label="test", message="fail")
        assert "\u2717" in r.icon


class TestDoctorReport:
    def test_counts(self):
        report = DoctorReport()
        report.add(CheckResult(status="pass", label="a", message="ok"))
        report.add(CheckResult(status="warn", label="b", message="w"))
        report.add(CheckResult(status="error", label="c", message="e"))
        report.add(CheckResult(status="pass", label="d", message="ok2"))
        assert report.passed == 2
        assert report.warnings == 1
        assert report.errors == 1

    def test_fixable(self):
        report = DoctorReport()
        report.add(CheckResult(status="warn", label="fix", message="fixme", fixable=True))
        report.add(CheckResult(status="pass", label="ok", message="ok", fixable=True))
        report.add(CheckResult(status="error", label="fix2", message="fixme2", fixable=True))
        # Only non-pass fixable items
        assert len(report.fixable) == 2


# ---------------------------------------------------------------------------
# check_config
# ---------------------------------------------------------------------------


class TestCheckConfig:
    def test_valid_config(self, agent_config_path):
        results = check_config(agent_config_path)
        statuses = [r.status for r in results]
        assert "pass" in statuses
        labels = [r.label for r in results]
        assert "Agent config" in labels

    def test_missing_config(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        results = check_config(missing)
        assert results[0].status == "error"
        assert "Not found" in results[0].message

    def test_invalid_config(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: valid: yaml: [[[")
        results = check_config(bad)
        # Should get an error result from parse failure
        assert any(r.status == "error" for r in results)

    def test_no_tools_warning(self, tmp_path):
        cfg = tmp_path / "agent.yaml"
        cfg.write_text('system_prompt: "test"\ntools: {}\nllm:\n  model: test\n')
        results = check_config(cfg)
        assert any(r.label == "Tools defined" and r.status == "warn" for r in results)


# ---------------------------------------------------------------------------
# check_llm_provider
# ---------------------------------------------------------------------------


class TestCheckLLMProvider:
    def test_no_key(self, monkeypatch, agent_config_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        # Remove secrets config so fallback doesn't find a key
        agent_config_path.write_text('system_prompt: "test"\nllm:\n  model: test\n')
        results = check_llm_provider(agent_config_path)
        assert results[0].status == "error"
        assert "No ANTHROPIC_API_KEY" in results[0].message

    def test_valid_key(self, monkeypatch, agent_config_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test123")
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.message = "API key is valid"
        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: mock_result,
        )
        results = check_llm_provider(agent_config_path)
        assert results[0].status == "pass"

    def test_invalid_key(self, monkeypatch, agent_config_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bad")
        mock_result = MagicMock()
        mock_result.ok = False
        mock_result.message = "Invalid API key (401 Unauthorized)"
        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: mock_result,
        )
        results = check_llm_provider(agent_config_path)
        assert results[0].status == "error"


# ---------------------------------------------------------------------------
# check_executors
# ---------------------------------------------------------------------------


class TestCheckExecutors:
    def test_importable_executor(self, agent_config_path, monkeypatch):
        # Patch importlib to succeed
        monkeypatch.setattr(
            "creel.doctor.importlib.import_module",
            lambda name: MagicMock(),
        )
        results = check_executors(agent_config_path)
        executor_results = [r for r in results if r.label.startswith("Executor:")]
        assert any(r.status == "pass" for r in executor_results)

    def test_missing_executor(self, agent_config_path, monkeypatch):
        def import_fail(name):
            raise ImportError(f"No module named '{name}'")

        monkeypatch.setattr(
            "creel.doctor.importlib.import_module",
            import_fail,
        )
        results = check_executors(agent_config_path)
        executor_results = [r for r in results if r.label.startswith("Executor:")]
        assert any(r.status == "warn" for r in executor_results)

    def test_no_config(self, tmp_path):
        results = check_executors(tmp_path / "missing.yaml")
        assert results[0].status == "warn"
        assert "Skipped" in results[0].message

    def test_docker_available(self, agent_config_path, monkeypatch):
        monkeypatch.setattr(
            "creel.doctor.importlib.import_module",
            lambda name: MagicMock(),
        )
        monkeypatch.setattr("creel.doctor.shutil.which", lambda cmd: "/usr/bin/docker")
        mock_proc = MagicMock(returncode=0)
        monkeypatch.setattr("creel.doctor.subprocess.run", lambda *a, **kw: mock_proc)
        results = check_executors(agent_config_path)
        docker_results = [r for r in results if r.label == "Docker"]
        assert any(r.status == "pass" for r in docker_results)

    def test_docker_not_installed(self, agent_config_path, monkeypatch):
        monkeypatch.setattr(
            "creel.doctor.importlib.import_module",
            lambda name: MagicMock(),
        )
        monkeypatch.setattr("creel.doctor.shutil.which", lambda cmd: None)
        results = check_executors(agent_config_path)
        docker_results = [r for r in results if r.label == "Docker"]
        assert any(r.status == "warn" for r in docker_results)


# ---------------------------------------------------------------------------
# check_channels
# ---------------------------------------------------------------------------


class TestCheckChannels:
    def test_no_channels(self, agent_config_path):
        results = check_channels(agent_config_path)
        assert any(r.label == "Channels" and r.status == "warn" for r in results)

    def test_imessage_channel(self, creel_home):
        cfg = creel_home / "agent.yaml"
        cfg.write_text(
            """\
system_prompt: "test"
channels:
  imessage:
    listen_to: "+1234567890"
"""
        )
        results = check_channels(cfg)
        assert any(r.label == "Channel: imessage" and r.status == "pass" for r in results)

    def test_no_config(self, tmp_path):
        results = check_channels(tmp_path / "missing.yaml")
        assert results[0].status == "warn"


# ---------------------------------------------------------------------------
# check_guardian
# ---------------------------------------------------------------------------


class TestCheckGuardian:
    def test_guardian_disabled(self, creel_home):
        cfg = creel_home / "agent.yaml"
        cfg.write_text(
            """\
system_prompt: "test"
guardian:
  enabled: false
"""
        )
        results = check_guardian(cfg)
        assert any(r.status == "warn" and "Disabled" in r.message for r in results)

    def test_guardian_enabled_with_policy(self, creel_home):
        cfg = creel_home / "agent.yaml"
        cfg.write_text(
            f"""\
system_prompt: "test"
guardian:
  enabled: true
  fast_classifier:
    enabled: false
  llm_judge:
    enabled: true
    model: claude-haiku-4-5-20251001
  policy:
    enabled: true
    policy_file: "{creel_home / "policies" / "default.yaml"}"
  audit:
    enabled: true
  drift:
    enabled: true
"""
        )
        results = check_guardian(cfg)
        policy_results = [r for r in results if r.label == "Guardian: policy engine"]
        assert any(r.status == "pass" for r in policy_results)

    def test_guardian_missing_policy_file(self, creel_home):
        cfg = creel_home / "agent.yaml"
        cfg.write_text(
            """\
system_prompt: "test"
guardian:
  enabled: true
  fast_classifier:
    enabled: false
  llm_judge:
    enabled: false
  policy:
    enabled: true
    policy_file: "/nonexistent/policy.yaml"
  audit:
    enabled: false
  drift:
    enabled: false
"""
        )
        results = check_guardian(cfg)
        policy_results = [r for r in results if r.label == "Guardian: policy engine"]
        assert any(r.status == "error" for r in policy_results)

    def test_no_config(self, tmp_path):
        results = check_guardian(tmp_path / "missing.yaml")
        assert results[0].status == "warn"


# ---------------------------------------------------------------------------
# check_security_posture
# ---------------------------------------------------------------------------


class TestCheckSecurityPosture:
    def test_age_key_exists(self, tmp_path, monkeypatch):
        age_dir = tmp_path / ".age"
        age_dir.mkdir()
        key_file = age_dir / "key.txt"
        key_file.write_text("AGE-SECRET-KEY-test")
        key_file.chmod(0o600)
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / ".creel"))

        results = check_security_posture()
        age_results = [r for r in results if r.label == "Age identity"]
        assert any(r.status == "pass" for r in age_results)

    def test_age_key_bad_permissions(self, tmp_path, monkeypatch):
        age_dir = tmp_path / ".age"
        age_dir.mkdir()
        key_file = age_dir / "key.txt"
        key_file.write_text("AGE-SECRET-KEY-test")
        key_file.chmod(0o644)
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / ".creel"))

        results = check_security_posture()
        perm_results = [r for r in results if r.label == "Age key permissions"]
        assert any(r.status == "warn" for r in perm_results)
        assert any(r.fixable for r in perm_results)

    def test_no_age_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(tmp_path / "nonexistent"))
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / ".creel"))

        results = check_security_posture()
        age_results = [r for r in results if r.label == "Age identity"]
        assert any(r.status == "warn" for r in age_results)


# ---------------------------------------------------------------------------
# check_sessions
# ---------------------------------------------------------------------------


class TestCheckSessions:
    def test_no_session_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / ".creel"))
        results = check_sessions()
        assert results[0].status == "warn"

    def test_empty_session_dir(self, creel_home):
        results = check_sessions()
        assert any(r.status == "pass" and "Empty" in r.message for r in results)

    def test_active_sessions(self, creel_home):
        sessions_dir = creel_home / "sessions"
        session = {
            "sender_id": "test",
            "session_id": "abc123",
            "messages": [],
            "last_active": time.time(),
        }
        (sessions_dir / "abc123.json").write_text(json.dumps(session))

        results = check_sessions()
        assert any(r.status == "pass" and "1 session(s)" in r.message for r in results)

    def test_stale_sessions(self, creel_home):
        sessions_dir = creel_home / "sessions"
        session = {
            "sender_id": "test",
            "session_id": "old123",
            "messages": [],
            "last_active": time.time() - 8 * 24 * 3600,  # 8 days ago
        }
        (sessions_dir / "old123.json").write_text(json.dumps(session))

        results = check_sessions()
        assert any(r.label == "Stale sessions" and r.status == "warn" for r in results)

    def test_corrupt_sessions(self, creel_home):
        sessions_dir = creel_home / "sessions"
        (sessions_dir / "bad.json").write_text("not json{{{")

        results = check_sessions()
        assert any(r.label == "Corrupt sessions" and r.status == "error" for r in results)


# ---------------------------------------------------------------------------
# apply_fixes
# ---------------------------------------------------------------------------


class TestApplyFixes:
    def test_fix_age_permissions(self, tmp_path, monkeypatch):
        age_dir = tmp_path / ".age"
        age_dir.mkdir()
        key_file = age_dir / "key.txt"
        key_file.write_text("AGE-SECRET-KEY-test")
        key_file.chmod(0o644)
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))

        report = DoctorReport()
        report.add(
            CheckResult(
                status="warn",
                label="Age key permissions",
                message="too open",
                fixable=True,
                fix_id=FIX_AGE_KEY_PERMISSIONS,
            )
        )
        actions = apply_fixes(report)
        assert len(actions) == 1
        assert "0600" in actions[0]
        assert key_file.stat().st_mode & 0o777 == 0o600

    def test_fix_stale_sessions(self, creel_home):
        sessions_dir = creel_home / "sessions"
        old_session = {
            "sender_id": "test",
            "session_id": "old",
            "messages": [],
            "last_active": time.time() - 8 * 24 * 3600,
        }
        (sessions_dir / "old.json").write_text(json.dumps(old_session))

        report = DoctorReport()
        report.add(
            CheckResult(
                status="warn",
                label="Stale sessions",
                message="1 stale",
                fixable=True,
                fix_id=FIX_STALE_SESSIONS,
            )
        )
        actions = apply_fixes(report)
        assert len(actions) == 1
        assert "stale" in actions[0].lower()
        assert not (sessions_dir / "old.json").exists()

    def test_fix_corrupt_sessions(self, creel_home):
        sessions_dir = creel_home / "sessions"
        (sessions_dir / "bad.json").write_text("not json")

        report = DoctorReport()
        report.add(
            CheckResult(
                status="error",
                label="Corrupt sessions",
                message="1 corrupt",
                fixable=True,
                fix_id=FIX_CORRUPT_SESSIONS,
            )
        )
        actions = apply_fixes(report)
        assert len(actions) == 1
        assert "corrupt" in actions[0].lower()
        assert not (sessions_dir / "bad.json").exists()

    def test_fix_create_session_dir(self, creel_home):
        sessions_dir = creel_home / "sessions"
        # Remove the sessions dir
        import shutil

        shutil.rmtree(sessions_dir)
        assert not sessions_dir.exists()

        report = DoctorReport()
        report.add(
            CheckResult(
                status="warn",
                label="Session store",
                message=f"Directory not found: {sessions_dir}",
                fixable=True,
                fix_id=FIX_SESSION_DIR_MISSING,
            )
        )
        actions = apply_fixes(report)
        assert len(actions) == 1
        assert sessions_dir.exists()


# ---------------------------------------------------------------------------
# run_doctor (integration)
# ---------------------------------------------------------------------------


class TestRunDoctor:
    def test_run_doctor_with_valid_config(self, agent_config_path, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.message = "API key is valid"
        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: mock_result,
        )
        monkeypatch.setattr(
            "creel.doctor.importlib.import_module",
            lambda name: MagicMock(),
        )
        monkeypatch.setattr("creel.doctor.shutil.which", lambda cmd: None)

        report = run_doctor(agent_config_path=agent_config_path)
        assert report.passed > 0

        captured = capsys.readouterr()
        assert "Summary" in captured.out

    def test_run_doctor_no_color(self, agent_config_path, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.message = "API key is valid"
        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: mock_result,
        )
        monkeypatch.setattr(
            "creel.doctor.importlib.import_module",
            lambda name: MagicMock(),
        )
        monkeypatch.setattr("creel.doctor.shutil.which", lambda cmd: None)

        run_doctor(agent_config_path=agent_config_path, no_color=True)
        captured = capsys.readouterr()
        # No ANSI escape codes
        assert "\033[" not in captured.out

    def test_run_doctor_fix_mode(self, creel_home, monkeypatch, capsys):
        agent_config_path = creel_home / "agent.yaml"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.message = "API key is valid"
        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: mock_result,
        )
        monkeypatch.setattr(
            "creel.doctor.importlib.import_module",
            lambda name: MagicMock(),
        )
        monkeypatch.setattr("creel.doctor.shutil.which", lambda cmd: None)

        # Create a stale session to fix
        sessions_dir = creel_home / "sessions"
        old_session = {
            "sender_id": "test",
            "session_id": "old",
            "messages": [],
            "last_active": time.time() - 8 * 24 * 3600,
        }
        (sessions_dir / "old.json").write_text(json.dumps(old_session))

        run_doctor(
            agent_config_path=agent_config_path,
            fix=True,
        )
        captured = capsys.readouterr()
        assert "Applying fixes" in captured.out


# ---------------------------------------------------------------------------
# cmd_doctor (CLI integration)
# ---------------------------------------------------------------------------


class TestCmdDoctor:
    def test_returns_zero_when_no_errors(self, agent_config_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.message = "API key is valid"
        monkeypatch.setattr(
            "creel.validation.validate_anthropic_key",
            lambda key: mock_result,
        )
        monkeypatch.setattr(
            "creel.doctor.importlib.import_module",
            lambda name: MagicMock(),
        )
        monkeypatch.setattr("creel.doctor.shutil.which", lambda cmd: None)

        report = run_doctor(
            agent_config_path=agent_config_path,
            no_color=True,
        )
        exit_code = 1 if report.errors > 0 else 0
        assert exit_code == 0

    def test_returns_one_when_errors(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("CREEL_HOME", str(tmp_path / ".creel"))
        monkeypatch.setenv("AGE_IDENTITY_FILE", str(tmp_path / "nonexistent"))

        report = run_doctor(
            agent_config_path=tmp_path / "nonexistent.yaml",
            no_color=True,
        )
        exit_code = 1 if report.errors > 0 else 0
        assert exit_code == 1
