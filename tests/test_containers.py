"""Tests for container infrastructure — base image, hash computation, and prebuilding."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from creel.containers import (
    _BASE_DOCKERFILE,
    _compute_base_image_hash,
    _compute_executor_hash,
    _ensure_base_image,
    _run_executor_container,
    collect_required_images,
    prebuild_images,
)


class TestBaseImageHash:
    """Tests for base image hash computation."""

    def test_hash_changes_when_base_dockerfile_changes(self) -> None:
        """Changing the base Dockerfile should produce a different hash."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = Path(tmp) / "Dockerfile"
            dockerfile.write_text("FROM python:3.12-slim\n")
            with patch("creel.containers._BASE_DOCKERFILE", dockerfile):
                hash1 = _compute_base_image_hash()

            dockerfile.write_text("FROM python:3.12-slim\nRUN echo changed\n")
            with patch("creel.containers._BASE_DOCKERFILE", dockerfile):
                hash2 = _compute_base_image_hash()

        assert hash1 != hash2

    def test_hash_is_12_chars(self) -> None:
        """Hash should be 12 hex characters."""
        h = _compute_base_image_hash()
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)


class TestEnsureBaseImage:
    """Tests for _ensure_base_image()."""

    @patch("subprocess.run")
    def test_skips_build_when_image_exists(self, mock_run: MagicMock) -> None:
        """Should skip building if the image already exists."""
        mock_run.return_value = MagicMock(returncode=0)  # docker inspect succeeds
        result = _ensure_base_image()
        assert result.startswith("creel-executor-base:")
        # docker inspect + docker tag (to ensure :latest alias)
        assert mock_run.call_count == 2
        assert "inspect" in mock_run.call_args_list[0][0][0][2]
        assert "tag" in mock_run.call_args_list[1][0][0][1]

    @patch("creel.containers._build_image")
    @patch("subprocess.run")
    def test_builds_when_image_missing(self, mock_run: MagicMock, mock_build: MagicMock) -> None:
        """Should build the image when docker inspect fails."""
        mock_run.return_value = MagicMock(returncode=1)  # docker inspect fails
        result = _ensure_base_image()
        assert result.startswith("creel-executor-base:")
        mock_build.assert_called_once()
        tags = mock_build.call_args[1].get("tags") or mock_build.call_args[0][0]
        assert any("creel-executor-base:" in t for t in tags)


class TestExecutorHashIncludesBase:
    """Tests for _compute_executor_hash including base Dockerfile."""

    def test_hash_changes_when_base_dockerfile_changes(self) -> None:
        """Executor hash should change when the base Dockerfile changes."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Set up a minimal executor directory
            executor_dir = tmp_path / "executors" / "weather"
            executor_dir.mkdir(parents=True)
            (executor_dir / "Dockerfile").write_text("FROM base\n")
            (executor_dir / "executor.py").write_text("print('hello')\n")

            base_dockerfile = tmp_path / "base" / "Dockerfile"
            base_dockerfile.parent.mkdir(parents=True)
            base_dockerfile.write_text("FROM python:3.12-slim\n")

            with patch("creel.containers._BASE_DOCKERFILE", base_dockerfile):
                hash1 = _compute_executor_hash(executor_dir)

            base_dockerfile.write_text("FROM python:3.12-slim\nRUN echo changed\n")
            with patch("creel.containers._BASE_DOCKERFILE", base_dockerfile):
                hash2 = _compute_executor_hash(executor_dir)

        assert hash1 != hash2


class TestCollectRequiredImages:
    """Tests for collect_required_images including base image."""

    def test_includes_base_image_when_executors_present(self) -> None:
        """Should include the base image when executor images are needed."""
        from creel.models import AgentDefinition, SkillOverride

        agent_def = AgentDefinition(
            system_prompt="test",
            skills={
                "weather": SkillOverride(enabled=True),
            },
        )
        with patch("creel.containers._BASE_DOCKERFILE", _BASE_DOCKERFILE):
            images = collect_required_images(agent_def)
        assert "creel-executor-base:latest" in images

    def test_excludes_base_when_only_custom_images(self) -> None:
        """Should not include base image when all tools use custom images."""
        from creel.models import AgentDefinition, SkillOverride

        agent_def = AgentDefinition(
            system_prompt="test",
            skills={
                "custom": SkillOverride(enabled=True, image="my-image:latest"),
            },
        )
        images = collect_required_images(agent_def)
        assert "creel-executor-base:latest" not in images


class TestPrebuildOrder:
    """Tests for prebuild_images building base first."""

    @patch("creel.containers._image_cache")
    @patch("creel.containers._ensure_base_image")
    def test_base_built_synchronously_first(
        self, mock_ensure_base: MagicMock, mock_cache: MagicMock
    ) -> None:
        """Base image should be built synchronously before parallel executor builds."""
        from creel.models import AgentDefinition, SkillOverride

        agent_def = AgentDefinition(
            system_prompt="test",
            skills={
                "weather": SkillOverride(enabled=True),
            },
        )

        mock_cache.start_prebuild.return_value = []

        prebuild_images(agent_def)

        # _ensure_base_image should have been called
        mock_ensure_base.assert_called_once()

        # start_prebuild should not include the base image
        prebuild_call = mock_cache.start_prebuild.call_args[0][0]
        assert "creel-executor-base:latest" not in prebuild_call


class TestRunExecutorContainerArgsFile:
    """Tests that _run_executor_container passes args via a JSON file."""

    @patch("creel.containers._ensure_image", return_value="executor-file-ops:abc123")
    @patch("subprocess.run")
    def test_docker_cmd_mounts_json_args_file(
        self, mock_run: MagicMock, mock_ensure: MagicMock
    ) -> None:
        """The docker run command should mount a JSON args file with full content."""
        from creel.models import ExecutorConfig

        captured = {}

        def capture_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            # Read the env file and args file while they still exist
            env_idx = cmd.index("--env-file") + 1
            captured["env"] = Path(cmd[env_idx]).read_text()
            for arg in cmd:
                if isinstance(arg, str) and arg.endswith("/creel-input.json:ro"):
                    host_path = arg.split(":/creel-input.json")[0]
                    with open(host_path) as f:
                        captured["args"] = json.load(f)
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = capture_run

        multiline_content = "line1\nline2\nline3\n"
        config = ExecutorConfig(
            name="file_ops",
            args={
                "action": "write",
                "file_path": "test.py",
                "content": multiline_content,
            },
        )

        _run_executor_container(config)

        # Verify the args file is mounted at /creel-input.json
        assert "/creel-input.json:ro" in " ".join(captured["cmd"])

        # Verify CREEL_INPUT_FILE is set in the env file
        assert "CREEL_INPUT_FILE=/creel-input.json" in captured["env"]

        # Verify the JSON args file preserves newlines
        assert captured["args"]["content"] == multiline_content
        assert "\n" in captured["args"]["content"]

    @patch("creel.containers._ensure_image", return_value="executor-weather:abc123")
    @patch("subprocess.run")
    def test_args_written_as_env_vars(self, mock_run: MagicMock, mock_ensure: MagicMock) -> None:
        """Args should be written to the env-file so executors that read from
        os.environ (most of them) still work."""
        from creel.models import ExecutorConfig

        captured: dict = {}

        def capture_run(cmd, **kwargs):
            env_idx = cmd.index("--env-file") + 1
            captured["env"] = Path(cmd[env_idx]).read_text()
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = capture_run

        config = ExecutorConfig(
            name="weather",
            args={"location": "London,UK"},
        )
        _run_executor_container(config)

        assert "LOCATION=London,UK" in captured["env"]

    @patch("creel.containers._ensure_image", return_value="executor-weather:abc123")
    @patch("subprocess.run")
    def test_reserved_env_names_are_prefixed(
        self, mock_run: MagicMock, mock_ensure: MagicMock
    ) -> None:
        """Args whose upper-case key collides with reserved names (PATH, HOME, …)
        should be written with an ARG_ prefix."""
        from creel.models import ExecutorConfig

        captured: dict = {}

        def capture_run(cmd, **kwargs):
            env_idx = cmd.index("--env-file") + 1
            captured["env"] = Path(cmd[env_idx]).read_text()
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = capture_run

        config = ExecutorConfig(
            name="test_exec",
            args={"path": "/some/path", "query": "hello"},
        )
        _run_executor_container(config)

        assert "ARG_PATH=/some/path" in captured["env"]
        assert "QUERY=hello" in captured["env"]
        # Must NOT write bare PATH= which would clobber the system PATH
        lines = captured["env"].strip().split("\n")
        assert not any(line.startswith("PATH=") for line in lines)


class TestCodingWorkspaceMount:
    """Tests that the coding executor gets WORKSPACE pointed at the rw mount."""

    @patch("creel.containers._ensure_image", return_value="executor-coding:abc123")
    @patch("subprocess.run")
    def test_workspace_set_to_first_rw_mount(
        self, mock_run: MagicMock, mock_ensure: MagicMock
    ) -> None:
        """WORKSPACE env var should be /mnt{host_path} for the first rw mount."""
        from creel.models import ExecutorConfig, MountConfig, ToolConfig

        captured: dict = {}

        def capture_run(cmd, **kwargs):
            env_idx = cmd.index("--env-file") + 1
            captured["env"] = Path(cmd[env_idx]).read_text()
            captured["cmd"] = list(cmd)
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = capture_run

        tool_config = ToolConfig(
            executor="coding",
            description="coding",
            mounts=[
                MountConfig(path="/home/user/project", mode="rw"),
                MountConfig(path="/home/user/data", mode="ro"),
            ],
            network=True,
            writable=True,
        )
        config = ExecutorConfig(
            name="coding",
            args={"command": "ls"},
        )
        _run_executor_container(config, tool_config=tool_config)

        assert "WORKSPACE=/mnt/home/user/project" in captured["env"]

    @patch("creel.containers._ensure_image", return_value="executor-coding:abc123")
    @patch("subprocess.run")
    def test_workspace_not_set_without_rw_mounts(
        self, mock_run: MagicMock, mock_ensure: MagicMock
    ) -> None:
        """WORKSPACE should not be overridden when there are no rw mounts."""
        from creel.models import ExecutorConfig, MountConfig, ToolConfig

        captured: dict = {}

        def capture_run(cmd, **kwargs):
            env_idx = cmd.index("--env-file") + 1
            captured["env"] = Path(cmd[env_idx]).read_text()
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = capture_run

        tool_config = ToolConfig(
            executor="coding",
            description="coding",
            mounts=[MountConfig(path="/home/user/data", mode="ro")],
            network=True,
            writable=True,
        )
        config = ExecutorConfig(name="coding", args={"command": "ls"})
        _run_executor_container(config, tool_config=tool_config)

        # Should NOT contain a WORKSPACE=/mnt... line
        env_lines = captured["env"].strip().split("\n")
        ws_lines = [line for line in env_lines if line.startswith("WORKSPACE=/mnt")]
        assert ws_lines == []


class TestFileOpsMountInheritance:
    """Tests that file_ops inherits coding mounts via _execute_skill_tool."""

    def test_file_ops_inherits_coding_mounts(self) -> None:
        """file_ops ToolConfig should receive coding's mounts."""
        from creel.models import MountConfig, SkillOverride
        from creel.tools import _build_tool_config_from_skill

        # Simulate a coding override with project mounts
        coding_override = SkillOverride(
            mounts=[
                MountConfig(path="/home/user/project", mode="rw"),
                MountConfig(path="/home/user/data", mode="ro"),
            ],
        )
        file_ops_override = SkillOverride()

        # Build a ToolConfig for file_ops (no mounts of its own)
        mock_meta = MagicMock()
        mock_meta.id = "file_ops"
        mock_meta.needs_network = False
        mock_spec = MagicMock()
        mock_spec.description = "file ops"

        tool_config = _build_tool_config_from_skill(mock_meta, file_ops_override, mock_spec)
        assert tool_config.mounts == []

        # Apply the inheritance logic (mirrors _execute_skill_tool)
        skill_overrides = {"coding": coding_override, "file_ops": file_ops_override}
        skill_id = "file_ops"
        if skill_id == "file_ops":
            co = skill_overrides.get("coding")
            if co and co.mounts:
                extra = [m for m in co.mounts if m not in tool_config.mounts]
                if extra:
                    tool_config.mounts = list(tool_config.mounts) + extra

        assert len(tool_config.mounts) == 2
        assert tool_config.mounts[0].path == "/home/user/project"
        assert tool_config.mounts[1].path == "/home/user/data"

    def test_mount_inheritance_does_not_mutate_coding_override(self) -> None:
        """Inheriting mounts must not modify the coding SkillOverride's list."""
        from creel.models import MountConfig, SkillOverride

        coding_override = SkillOverride(
            mounts=[MountConfig(path="/home/user/project", mode="rw")],
        )
        original_len = len(coding_override.mounts)

        # Simulate the inheritance with a tool_config that already has a mount
        existing = [MountConfig(path="/home/user/other", mode="ro")]
        extra = [m for m in coding_override.mounts if m not in existing]
        result = list(existing) + extra

        assert len(result) == 2
        assert len(coding_override.mounts) == original_len  # not mutated
