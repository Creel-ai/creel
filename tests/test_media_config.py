"""Tests for MediaConfig and its integration with AgentDefinition / ChatServer (MEDIA-008)."""

from __future__ import annotations

from pathlib import Path

import yaml

from taskrunner.models import (
    AgentConfig,
    AgentDefinition,
    ChannelsConfig,
    LLMConfig,
    MediaConfig,
    SessionConfig,
    TranscriptionConfig,
    VisionConfig,
    WorkspaceConfig,
    load_agent_config,
)

# ---------------------------------------------------------------------------
# MediaConfig model tests
# ---------------------------------------------------------------------------


class TestMediaConfigDefaults:
    def test_defaults(self) -> None:
        cfg = MediaConfig()
        assert cfg.enabled is True
        assert cfg.storage_dir == "~/.creel/media"
        assert cfg.max_file_size_mb == 20
        assert cfg.retention_days == 30
        assert cfg.transcription.backend == "openai"
        assert cfg.transcription.model == "whisper-1"
        assert cfg.transcription.api_key is None
        assert cfg.vision.max_pixels == 2048
        assert cfg.vision.quality == 85

    def test_transcription_config_defaults(self) -> None:
        cfg = TranscriptionConfig()
        assert cfg.backend == "openai"
        assert cfg.model == "whisper-1"
        assert cfg.api_key is None

    def test_vision_config_defaults(self) -> None:
        cfg = VisionConfig()
        assert cfg.max_pixels == 2048
        assert cfg.quality == 85


class TestMediaConfigCustom:
    def test_custom_values(self) -> None:
        cfg = MediaConfig(
            enabled=True,
            storage_dir="/custom/media",
            max_file_size_mb=50,
            retention_days=7,
            transcription=TranscriptionConfig(
                backend="local",
                model="whisper-large",
                api_key="sk-test",
            ),
            vision=VisionConfig(max_pixels=1024, quality=70),
        )
        assert cfg.storage_dir == "/custom/media"
        assert cfg.max_file_size_mb == 50
        assert cfg.retention_days == 7
        assert cfg.transcription.backend == "local"
        assert cfg.transcription.model == "whisper-large"
        assert cfg.transcription.api_key == "sk-test"
        assert cfg.vision.max_pixels == 1024
        assert cfg.vision.quality == 70

    def test_disabled(self) -> None:
        cfg = MediaConfig(enabled=False)
        assert cfg.enabled is False


# ---------------------------------------------------------------------------
# AgentDefinition integration
# ---------------------------------------------------------------------------


class TestAgentDefinitionMedia:
    def _base_kwargs(self, tmp_path: Path) -> dict:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        return dict(
            system_prompt="Test.",
            llm=LLMConfig(model="test", max_tokens=100),
            agent=AgentConfig(max_turns=3),
            session=SessionConfig(
                sessions_dir=str(sessions_dir),
                max_history=50,
                summarize_on_trim=False,
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "ws")),
            channels=ChannelsConfig(),
        )

    def test_media_none_by_default(self, tmp_path: Path) -> None:
        agent_def = AgentDefinition(**self._base_kwargs(tmp_path))
        assert agent_def.media is None

    def test_media_config_set(self, tmp_path: Path) -> None:
        kwargs = self._base_kwargs(tmp_path)
        kwargs["media"] = MediaConfig(enabled=True, retention_days=14)
        agent_def = AgentDefinition(**kwargs)
        assert agent_def.media is not None
        assert agent_def.media.retention_days == 14

    def test_media_disabled(self, tmp_path: Path) -> None:
        kwargs = self._base_kwargs(tmp_path)
        kwargs["media"] = MediaConfig(enabled=False)
        agent_def = AgentDefinition(**kwargs)
        assert agent_def.media is not None
        assert agent_def.media.enabled is False


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


class TestLoadAgentConfigMedia:
    def _write_config(self, tmp_path: Path, config: dict) -> Path:
        path = tmp_path / "agent.yaml"
        path.write_text(yaml.dump(config))
        return path

    def test_load_without_media_section(self, tmp_path: Path) -> None:
        """Existing configs without a media section still load fine."""
        config = {
            "system_prompt": "Hello.",
            "llm": {"model": "test", "max_tokens": 100},
        }
        agent_def = load_agent_config(self._write_config(tmp_path, config))
        assert agent_def.media is None

    def test_load_with_media_enabled(self, tmp_path: Path) -> None:
        config = {
            "system_prompt": "Hello.",
            "media": {
                "enabled": True,
                "storage_dir": "/tmp/test-media",
                "max_file_size_mb": 10,
                "retention_days": 7,
                "transcription": {"backend": "local", "model": "whisper-large"},
                "vision": {"max_pixels": 1024, "quality": 70},
            },
        }
        agent_def = load_agent_config(self._write_config(tmp_path, config))
        assert agent_def.media is not None
        assert agent_def.media.enabled is True
        assert agent_def.media.storage_dir == "/tmp/test-media"
        assert agent_def.media.max_file_size_mb == 10
        assert agent_def.media.transcription.backend == "local"
        assert agent_def.media.vision.max_pixels == 1024

    def test_load_with_media_disabled(self, tmp_path: Path) -> None:
        config = {
            "system_prompt": "Hello.",
            "media": {"enabled": False},
        }
        agent_def = load_agent_config(self._write_config(tmp_path, config))
        assert agent_def.media is not None
        assert agent_def.media.enabled is False

    def test_load_with_media_minimal(self, tmp_path: Path) -> None:
        """Media section with just enabled: true uses all defaults."""
        config = {
            "system_prompt": "Hello.",
            "media": {"enabled": True},
        }
        agent_def = load_agent_config(self._write_config(tmp_path, config))
        assert agent_def.media is not None
        assert agent_def.media.transcription.backend == "openai"
        assert agent_def.media.vision.max_pixels == 2048


# ---------------------------------------------------------------------------
# ChatServer integration
# ---------------------------------------------------------------------------


class TestChatServerMediaConfig:
    def _base_kwargs(self, tmp_path: Path) -> dict:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        return dict(
            system_prompt="Test.",
            llm=LLMConfig(model="test", max_tokens=100),
            agent=AgentConfig(max_turns=3),
            session=SessionConfig(
                sessions_dir=str(sessions_dir),
                max_history=50,
                summarize_on_trim=False,
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "ws")),
            channels=ChannelsConfig(),
        )

    def test_media_none_services_disabled(self, tmp_path: Path) -> None:
        """When media is None, all media services should be None."""
        from taskrunner.chat import ChatServer

        agent_def = AgentDefinition(**self._base_kwargs(tmp_path))
        server = ChatServer(agent_def)
        assert server._media_store is None
        assert server._transcription is None
        assert server._vision is None

    def test_media_disabled_services_disabled(self, tmp_path: Path) -> None:
        """When media.enabled is False, all media services should be None."""
        from taskrunner.chat import ChatServer

        kwargs = self._base_kwargs(tmp_path)
        kwargs["media"] = MediaConfig(enabled=False)
        agent_def = AgentDefinition(**kwargs)
        server = ChatServer(agent_def)
        assert server._media_store is None
        assert server._transcription is None
        assert server._vision is None

    def test_media_enabled_services_created(self, tmp_path: Path) -> None:
        """When media.enabled is True, all media services are initialized."""
        from taskrunner.chat import ChatServer
        from taskrunner.services.media_store import MediaStore
        from taskrunner.services.transcription import TranscriptionService
        from taskrunner.services.vision import VisionProcessor

        kwargs = self._base_kwargs(tmp_path)
        kwargs["media"] = MediaConfig(
            enabled=True,
            storage_dir=str(tmp_path / "media"),
            max_file_size_mb=10,
            retention_days=7,
            transcription=TranscriptionConfig(backend="local", api_key="sk-test"),
            vision=VisionConfig(max_pixels=1024, quality=70),
        )
        agent_def = AgentDefinition(**kwargs)
        server = ChatServer(agent_def)

        assert isinstance(server._media_store, MediaStore)
        assert isinstance(server._transcription, TranscriptionService)
        assert isinstance(server._vision, VisionProcessor)

        # Verify config values are passed through
        assert server._media_store._base_dir == tmp_path / "media"
        assert server._media_store._max_file_size == 10 * 1024 * 1024
        assert server._media_store._retention_days == 7
        assert server._transcription._backend == "local"
        assert server._transcription._api_key == "sk-test"
        assert server._vision._max_pixels == 1024
        assert server._vision._quality == 70

    def test_media_disabled_attachments_ignored(self, tmp_path: Path) -> None:
        """Attachments are silently ignored when media is disabled."""

        from taskrunner.channels.message import Attachment, AttachmentType
        from taskrunner.chat import ChatServer

        agent_def = AgentDefinition(**self._base_kwargs(tmp_path))
        server = ChatServer(agent_def)

        att = Attachment(
            type=AttachmentType.IMAGE,
            data=b"\x00" * 100,
            mime_type="image/png",
        )

        text, blocks = server._process_attachments("hello", [att], "user1")
        assert text == "hello"
        assert blocks == []

    def test_storage_dir_tilde_expanded(self, tmp_path: Path) -> None:
        """storage_dir with ~ should be expanded to home directory."""
        from taskrunner.chat import ChatServer

        kwargs = self._base_kwargs(tmp_path)
        kwargs["media"] = MediaConfig(enabled=True, storage_dir="~/test-media")
        agent_def = AgentDefinition(**kwargs)
        server = ChatServer(agent_def)
        assert server._media_store is not None
        assert "~" not in str(server._media_store._base_dir)
        assert str(server._media_store._base_dir) == str(Path.home() / "test-media")
