"""Startup validation — fail fast if secrets or config are broken."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from taskrunner.secrets import decrypt_env_file

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

    # Collect all secret paths
    secrets_paths: list[tuple[str, str]] = []  # (label, path)

    if agent_def.llm.secrets:
        secrets_paths.append(("llm.secrets", agent_def.llm.secrets))

    for tool_name, tool_cfg in agent_def.tools.items():
        if tool_cfg.secrets:
            secrets_paths.append((f"tools.{tool_name}.secrets", tool_cfg.secrets))

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

    # Check each secrets file
    for label, path in secrets_paths:
        p = Path(path)
        if not p.exists():
            errors.append(f"{label}: file not found: {path}")
            continue

        # Try to decrypt
        if not errors:  # Only try if identity exists
            try:
                decrypt_env_file(path)
                logger.debug("Validated %s: %s ✓", label, path)
            except Exception as e:
                errors.append(f"{label}: failed to decrypt {path}: {e}")

    if errors:
        msg = "Startup secrets validation failed:\n" + "\n".join(f"  • {e}" for e in errors)
        raise SecretsValidationError(msg)

    logger.info("All %d secret file(s) validated successfully", len(secrets_paths))
