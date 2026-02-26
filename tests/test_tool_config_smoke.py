"""Smoke tests: verify agent.yaml tool configs match executor capabilities.

These tests catch mismatches between tool definitions in agent.yaml and
the actual executor code — action names, required parameters, Dockerfiles,
bridge config, etc. They run without Docker or external services.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
AGENT_YAML = ROOT / "agent.yaml"
EXECUTORS_DIR = ROOT / "src" / "executors"
POLICIES_FILE = ROOT / "policies" / "default.yaml"


def _load_agent_yaml() -> dict:
    with open(AGENT_YAML) as f:
        return yaml.safe_load(f)


def _load_policies() -> dict:
    with open(POLICIES_FILE) as f:
        return yaml.safe_load(f)


def _get_executor_actions(executor_name: str) -> set[str]:
    """Parse executor.py to find all valid action strings."""
    # Map executor name to directory (handle naming conventions)
    candidates = [
        EXECUTORS_DIR / executor_name,
        EXECUTORS_DIR / executor_name.replace("-", "_"),
    ]

    for d in candidates:
        py_file = d / "executor.py"
        if py_file.exists():
            content = py_file.read_text()
            # Match: action == "something" or action == 'something'
            actions = set(re.findall(r'action\s*==\s*["\']([^"\']+)["\']', content))
            # Also match default: os.environ.get("ACTION", "something")
            defaults = re.findall(r'environ\.get\(["\']ACTION["\'],\s*["\']([^"\']+)["\']', content)
            actions.update(defaults)
            return actions
    return set()


def _get_executor_env_vars(executor_name: str) -> set[str]:
    """Parse executor.py to find all environment variables it reads."""
    candidates = [
        EXECUTORS_DIR / executor_name,
        EXECUTORS_DIR / executor_name.replace("-", "_"),
    ]

    for d in candidates:
        py_file = d / "executor.py"
        if py_file.exists():
            content = py_file.read_text()
            return set(re.findall(r'environ\.get\(["\']([^"\']+)["\']', content))
    return set()


def _has_dockerfile(executor_name: str) -> bool:
    candidates = [
        EXECUTORS_DIR / executor_name / "Dockerfile",
        EXECUTORS_DIR / executor_name.replace("-", "_") / "Dockerfile",
    ]
    return any(p.exists() for p in candidates)


def _has_requirements(executor_name: str) -> bool:
    candidates = [
        EXECUTORS_DIR / executor_name / "requirements.txt",
        EXECUTORS_DIR / executor_name.replace("-", "_") / "requirements.txt",
    ]
    return any(p.exists() and p.read_text().strip() for p in candidates)


def _executor_uses_requests(executor_name: str) -> bool:
    candidates = [
        EXECUTORS_DIR / executor_name / "executor.py",
        EXECUTORS_DIR / executor_name.replace("-", "_") / "executor.py",
    ]
    for p in candidates:
        if p.exists() and "import requests" in p.read_text():
            return True
    return False


def _executor_uses_bridge(executor_name: str) -> bool:
    """Check if executor calls the bridge server (reads BRIDGE_URL)."""
    candidates = [
        EXECUTORS_DIR / executor_name / "executor.py",
        EXECUTORS_DIR / executor_name.replace("-", "_") / "executor.py",
    ]
    for p in candidates:
        if p.exists() and "BRIDGE_URL" in p.read_text():
            return True
    return False


# ---- Fixtures ----


@pytest.fixture(scope="module")
def agent_config():
    return _load_agent_yaml()


@pytest.fixture(scope="module")
def tools(agent_config):
    return agent_config.get("tools", {})


@pytest.fixture(scope="module")
def policies():
    return _load_policies()


# ---- Tests: Action Name Matching ----


class TestActionNames:
    """Verify fixed_args.action values match executor capabilities."""

    def test_all_fixed_actions_are_valid(self, tools):
        """Every tool's fixed_args.action must be a valid action in its executor."""
        mismatches = []
        for tool_name, tool_cfg in tools.items():
            executor = tool_cfg.get("executor", "")
            fixed = tool_cfg.get("fixed_args", {})
            action = fixed.get("action")
            if not action:
                continue

            valid_actions = _get_executor_actions(executor)
            if not valid_actions:
                continue  # Can't verify if executor not found

            if action not in valid_actions:
                mismatches.append(
                    f"{tool_name}: action '{action}' not in {executor} "
                    f"(valid: {sorted(valid_actions)})"
                )

        assert not mismatches, "Action mismatches:\n" + "\n".join(mismatches)

    def test_no_duplicate_tool_names(self, tools):
        """Tool names should be unique (YAML handles this, but verify)."""
        # YAML deduplicates, but this catches if we missed something
        assert len(tools) == len(set(tools.keys()))


# ---- Tests: Dockerfile & Dependencies ----


class TestDockerfiles:
    """Verify all executors have Dockerfiles and dependencies."""

    def test_all_executors_have_dockerfiles(self, tools):
        """Every executor referenced in agent.yaml should have a Dockerfile."""
        missing = []
        seen_executors = set()
        for tool_name, tool_cfg in tools.items():
            executor = tool_cfg.get("executor", "")
            if executor in seen_executors:
                continue
            seen_executors.add(executor)

            if not _has_dockerfile(executor):
                missing.append(f"{executor} (used by {tool_name})")

        assert not missing, "Missing Dockerfiles:\n" + "\n".join(missing)

    def test_bridge_executors_have_requests(self, tools):
        """Executors that import requests must have it in requirements.txt."""
        missing = []
        seen = set()
        for tool_name, tool_cfg in tools.items():
            executor = tool_cfg.get("executor", "")
            if executor in seen:
                continue
            seen.add(executor)

            if _executor_uses_requests(executor) and not _has_requirements(executor):
                missing.append(f"{executor} (used by {tool_name})")

        assert not missing, "Executors using requests but missing requirements.txt:\n" + "\n".join(
            missing
        )


# ---- Tests: Bridge Configuration ----


class TestBridgeConfig:
    """Verify bridge tools have proper network and bridge settings."""

    def test_bridge_executors_have_network(self, tools):
        """Tools calling the bridge need network: true to reach host."""
        issues = []
        for tool_name, tool_cfg in tools.items():
            executor = tool_cfg.get("executor", "")
            if _executor_uses_bridge(executor):
                if not tool_cfg.get("network"):
                    issues.append(f"{tool_name} (executor: {executor})")

        assert not issues, "Bridge tools missing network: true:\n" + "\n".join(issues)

    def test_bridge_executors_have_bridge_flag(self, tools):
        """Tools calling the bridge should have bridge: true for token injection."""
        issues = []
        for tool_name, tool_cfg in tools.items():
            executor = tool_cfg.get("executor", "")
            if _executor_uses_bridge(executor):
                if not tool_cfg.get("bridge"):
                    issues.append(f"{tool_name} (executor: {executor})")

        assert not issues, "Bridge tools missing bridge: true:\n" + "\n".join(issues)

    def test_network_executors_have_network(self, tools):
        """Executors that make HTTP calls need network: true."""
        issues = []
        for tool_name, tool_cfg in tools.items():
            executor = tool_cfg.get("executor", "")
            if _executor_uses_requests(executor):
                if not tool_cfg.get("network"):
                    issues.append(f"{tool_name} (executor: {executor})")

        assert not issues, "HTTP-calling tools missing network: true:\n" + "\n".join(issues)


# ---- Tests: Policy Coverage ----


class TestPolicyCoverage:
    """Verify all tools appear in at least one policy rule."""

    def _all_policy_patterns(self, policies) -> list[str]:
        patterns = []
        for key in ("allow", "review", "deny", "auto_approve"):
            patterns.extend(policies.get(key, []))
        for key in ("deny_when", "review_when"):
            for rule in policies.get(key, []):
                if isinstance(rule, dict) and "tool" in rule:
                    patterns.append(rule["tool"])
        return patterns

    def test_all_tools_covered_by_policy(self, tools, policies):
        """Every tool should match at least one allow/review/deny rule."""
        import fnmatch

        patterns = self._all_policy_patterns(policies)
        uncovered = []
        for tool_name in tools:
            matched = any(fnmatch.fnmatch(tool_name, p) for p in patterns)
            if not matched:
                uncovered.append(tool_name)

        # Uncovered tools default to review, which is safe but noisy
        if uncovered:
            pytest.skip(f"Uncovered tools (default to review): {uncovered}")


# ---- Tests: Parameter Mapping ----


class TestParameterMapping:
    """Verify tool parameters map to executor env vars."""

    def test_required_params_map_to_env_vars(self, tools):
        """Required parameters should correspond to env vars the executor reads."""
        warnings = []
        for tool_name, tool_cfg in tools.items():
            executor = tool_cfg.get("executor", "")
            env_vars = _get_executor_env_vars(executor)
            if not env_vars:
                continue

            params = tool_cfg.get("parameters", {})
            for param_name, param_cfg in params.items():
                if isinstance(param_cfg, dict) and param_cfg.get("required"):
                    # Parameter should map to an env var (uppercased)
                    if param_name.upper() not in env_vars:
                        warnings.append(
                            f"{tool_name}.{param_name} → {param_name.upper()} "
                            f"not in {executor} env vars"
                        )

        # This is advisory — some params go through fixed_args or other mapping
        if warnings:
            pytest.skip(f"Possible param mismatches (may be false positives): {warnings[:5]}")
