"""Startup validation — fail fast if secrets or config are broken."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from creel import paths
from creel.secrets import decrypt_env_file

logger = logging.getLogger(__name__)


class SecretsValidationError(RuntimeError):
    """Raised when startup secrets validation fails."""


def validate_secrets(agent_def) -> None:
    """Verify all referenced .enc secret files exist and are decryptable.

    Checks:
    - agent_def.llm.secrets
    - Each tool's secrets in agent_def.tools

    Raises SecretsValidationError with a clear message on failure.
    """
    errors: list[str] = []

    # Collect all secret paths, distinguishing required (LLM) from optional
    required_secrets: list[tuple[str, str]] = []  # (label, path)
    optional_secrets: list[tuple[str, str]] = []  # (label, path)

    if agent_def.llm.secrets:
        required_secrets.append(("llm.secrets", agent_def.llm.secrets))

    for tool_name, tool_cfg in agent_def.tools.items():
        if tool_cfg.secrets:
            optional_secrets.append((f"tools.{tool_name}.secrets", tool_cfg.secrets))

    # Check channel secrets (e.g. telegram)
    for channel_id in agent_def.channels.configured_channels():
        channel_cfg = agent_def.channels.get_channel_config(channel_id)
        if channel_cfg and channel_cfg.get("secrets"):
            optional_secrets.append((f"channels.{channel_id}.secrets", channel_cfg["secrets"]))

    secrets_paths = required_secrets + optional_secrets

    if not secrets_paths:
        logger.debug("No secrets files referenced, skipping validation")
        return

    # Check age identity file exists
    identity_path = os.environ.get(
        "AGE_IDENTITY_FILE",
        str(Path.home() / ".age" / "key.txt"),
    )
    if not Path(identity_path).exists():
        errors.append(f"Age identity file not found: {identity_path}")

    required_labels = {label for label, _ in required_secrets}

    # Check each secrets file — resolve relative paths against creel_home()
    for label, path in secrets_paths:
        p = Path(path)
        if not p.is_absolute():
            p = paths.creel_home() / p
        if not p.exists():
            if label in required_labels:
                errors.append(f"{label}: secrets file not found: {path}")
            else:
                logger.warning(
                    "%s: secrets file not found: %s (tool will be unavailable)", label, path
                )
            continue

        # Try to decrypt
        if not errors:  # Only try if identity exists
            try:
                decrypt_env_file(str(p))
                logger.debug("Validated %s: %s ✓", label, p)
            except Exception as e:
                errors.append(f"{label}: failed to decrypt {path}: {e}")

    if errors:
        msg = "Startup secrets validation failed:\n" + "\n".join(f"  • {e}" for e in errors)
        raise SecretsValidationError(msg)

    logger.info("All %d secret file(s) validated successfully", len(secrets_paths))
