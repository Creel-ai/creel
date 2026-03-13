"""Tests for agent deployment controls (versioning, deploy, rollback)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from creel.deploy import (
    DeploymentHistory,
    DeploymentRecord,
    create_snapshot,
    deploy,
    get_history,
    rollback,
    validate_config,
)


@pytest.fixture
def creel_home(tmp_path: Path) -> Path:
    """Create a minimal creel home with a valid agent.yaml."""
    home = tmp_path / "creel_home"
    home.mkdir()

    config = {
        "system_prompt": "You are a helpful assistant.",
        "tools": {},
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 300},
    }
    (home / "agent.yaml").write_text(yaml.dump(config))

    policies = home / "policies"
    policies.mkdir()
    (policies / "default.yaml").write_text(yaml.dump({"rules": []}))

    tasks = home / "tasks"
    tasks.mkdir()
    (tasks / "morning.yaml").write_text(
        yaml.dump(
            {
                "name": "morning",
                "schedule": "0 8 * * *",
                "prompt": "Good morning",
                "output": {"type": "stdout", "to": "console"},
            }
        )
    )

    return home


@pytest.fixture
def deploy_dir(tmp_path: Path) -> Path:
    d = tmp_path / "deployments"
    d.mkdir()
    return d


class TestDeploymentRecord:
    def test_label_with_tag(self) -> None:
        rec = DeploymentRecord(
            version=1, tag="v1.0", timestamp="2026-01-01T00:00:00+00:00", config_hash="abc123"
        )
        assert rec.label == "v1.0"

    def test_label_without_tag(self) -> None:
        rec = DeploymentRecord(
            version=3, timestamp="2026-01-01T00:00:00+00:00", config_hash="abc123"
        )
        assert rec.label == "v3"


class TestValidateConfig:
    def test_valid_config(self, creel_home: Path) -> None:
        errors = validate_config(creel_home / "agent.yaml")
        assert errors == []

    def test_missing_file(self, tmp_path: Path) -> None:
        errors = validate_config(tmp_path / "nonexistent.yaml")
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("system_prompt: [\ninvalid yaml")
        errors = validate_config(bad)
        assert len(errors) == 1
        assert "YAML" in errors[0] or "parse" in errors[0].lower()

    def test_schema_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        # Missing required 'system_prompt'
        bad.write_text(yaml.dump({"llm": {"model": "test"}}))
        errors = validate_config(bad)
        assert len(errors) == 1
        assert "validation" in errors[0].lower() or "system_prompt" in errors[0].lower()

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a\n- list\n")
        errors = validate_config(bad)
        assert len(errors) == 1
        assert "mapping" in errors[0].lower()


class TestCreateSnapshot:
    def test_first_snapshot(self, creel_home: Path, deploy_dir: Path) -> None:
        rec = create_snapshot(creel_home, deploy_dir)
        assert rec.version == 1
        assert rec.tag is None
        assert rec.config_hash

        # Snapshot directory should contain agent.yaml
        snap = deploy_dir / "snapshots" / "1" / "agent.yaml"
        assert snap.exists()

        # History should be persisted
        history = json.loads((deploy_dir / "history.json").read_text())
        assert history["active_version"] == 1
        assert len(history["records"]) == 1

    def test_increments_version(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir)
        rec2 = create_snapshot(creel_home, deploy_dir)
        assert rec2.version == 2

    def test_with_tag(self, creel_home: Path, deploy_dir: Path) -> None:
        rec = create_snapshot(creel_home, deploy_dir, tag="v1.0")
        assert rec.tag == "v1.0"
        assert rec.label == "v1.0"

    def test_duplicate_tag_rejected(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir, tag="release-1")
        with pytest.raises(ValueError, match="already exists"):
            create_snapshot(creel_home, deploy_dir, tag="release-1")

    def test_with_message(self, creel_home: Path, deploy_dir: Path) -> None:
        rec = create_snapshot(creel_home, deploy_dir, message="initial deploy")
        assert rec.message == "initial deploy"

    def test_snapshots_all_config_files(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir)
        snap = deploy_dir / "snapshots" / "1"
        assert (snap / "agent.yaml").exists()
        assert (snap / "policies" / "default.yaml").exists()
        assert (snap / "tasks" / "morning.yaml").exists()


class TestDeploy:
    def test_deploy_restores_config(self, creel_home: Path, deploy_dir: Path) -> None:
        # Create v1 snapshot
        create_snapshot(creel_home, deploy_dir, tag="v1")

        # Modify config
        config_path = creel_home / "agent.yaml"
        original_content = config_path.read_text()
        modified = yaml.safe_load(original_content)
        modified["system_prompt"] = "Modified prompt"
        config_path.write_text(yaml.dump(modified))

        # Create v2 snapshot
        create_snapshot(creel_home, deploy_dir, tag="v2")

        # Deploy v1 — should restore original config
        rec = deploy(creel_home, deploy_dir, version=1)
        assert rec.version == 1

        restored = yaml.safe_load(config_path.read_text())
        assert restored["system_prompt"] == "You are a helpful assistant."

    def test_deploy_nonexistent_version(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir)
        with pytest.raises(ValueError, match="not found"):
            deploy(creel_home, deploy_dir, version=99)

    def test_deploy_updates_active_version(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir)
        create_snapshot(creel_home, deploy_dir)
        deploy(creel_home, deploy_dir, version=1)

        history = DeploymentHistory(**json.loads((deploy_dir / "history.json").read_text()))
        assert history.active_version == 1


class TestRollback:
    def test_rollback_to_previous(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir, tag="v1")

        # Modify and create v2
        config_path = creel_home / "agent.yaml"
        modified = yaml.safe_load(config_path.read_text())
        modified["system_prompt"] = "Changed"
        config_path.write_text(yaml.dump(modified))
        create_snapshot(creel_home, deploy_dir, tag="v2")

        rec = rollback(creel_home, deploy_dir)
        assert rec.version == 1

        restored = yaml.safe_load(config_path.read_text())
        assert restored["system_prompt"] == "You are a helpful assistant."

    def test_rollback_to_tag(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir, tag="stable")

        modified = yaml.safe_load((creel_home / "agent.yaml").read_text())
        modified["system_prompt"] = "Changed"
        (creel_home / "agent.yaml").write_text(yaml.dump(modified))
        create_snapshot(creel_home, deploy_dir)
        create_snapshot(creel_home, deploy_dir)

        rec = rollback(creel_home, deploy_dir, target_tag="stable")
        assert rec.version == 1

    def test_rollback_to_version_string(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir)
        create_snapshot(creel_home, deploy_dir)
        create_snapshot(creel_home, deploy_dir)

        rec = rollback(creel_home, deploy_dir, target_tag="v1")
        assert rec.version == 1

    def test_rollback_empty_history(self, creel_home: Path, deploy_dir: Path) -> None:
        with pytest.raises(ValueError, match="No deployment history"):
            rollback(creel_home, deploy_dir)

    def test_rollback_at_earliest(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir)
        # Active is v1 which is the earliest — can't go further back
        with pytest.raises(ValueError, match="earliest"):
            rollback(creel_home, deploy_dir)

    def test_rollback_unknown_tag(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir)
        with pytest.raises(ValueError, match="No deployment found"):
            rollback(creel_home, deploy_dir, target_tag="nonexistent")


class TestGetHistory:
    def test_empty_history(self, deploy_dir: Path) -> None:
        rows = get_history(deploy_dir)
        assert rows == []

    def test_history_rows(self, creel_home: Path, deploy_dir: Path) -> None:
        create_snapshot(creel_home, deploy_dir, tag="v1.0", message="first")
        create_snapshot(creel_home, deploy_dir, message="second")

        rows = get_history(deploy_dir)
        assert len(rows) == 2
        assert rows[0]["version"] == 1
        assert rows[0]["tag"] == "v1.0"
        assert rows[0]["message"] == "first"
        assert rows[0]["active"] is False  # v2 is now active
        assert rows[1]["version"] == 2
        assert rows[1]["active"] is True
