"""Secrets management using age encryption (via pyrage)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pyrage


def decrypt_env_file(enc_path: str | Path, identity_path: str | None = None) -> dict[str, str]:
    """Decrypt an age-encrypted .env file and return key-value pairs.

    Args:
        enc_path: Path to the .enc file.
        identity_path: Path to the age identity (private key) file.
            Defaults to AGE_IDENTITY_FILE env var or ~/.age/key.txt.

    Returns:
        Dictionary of environment variable key-value pairs.
    """
    enc_path = Path(enc_path)
    if not enc_path.exists():
        raise FileNotFoundError(f"Encrypted file not found: {enc_path}")

    if identity_path is None:
        identity_path = os.environ.get(
            "AGE_IDENTITY_FILE",
            str(Path.home() / ".age" / "key.txt"),
        )

    identity_path = Path(identity_path)
    if not identity_path.exists():
        raise FileNotFoundError(f"Age identity file not found: {identity_path}")

    # key.txt contains comment lines; extract just the secret key line
    identity_line = next(
        line for line in identity_path.read_text().splitlines()
        if line.startswith("AGE-SECRET-KEY-")
    )
    identity = pyrage.x25519.Identity.from_str(identity_line)
    ciphertext = enc_path.read_bytes()
    plaintext = pyrage.decrypt(ciphertext, [identity])

    return _parse_env(plaintext.decode("utf-8"))


def encrypt_env_file(
    env_path: str | Path,
    recipient_path: str | None = None,
    output_path: str | Path | None = None,
) -> Path:
    """Encrypt a plaintext .env file with age.

    Args:
        env_path: Path to the plaintext .env file.
        recipient_path: Path to the age recipient (public key) file.
            Defaults to AGE_RECIPIENT_FILE env var or ~/.age/key.pub.
        output_path: Where to write the encrypted file.
            Defaults to env_path with .enc appended.

    Returns:
        Path to the encrypted file.
    """
    env_path = Path(env_path)
    if not env_path.exists():
        raise FileNotFoundError(f"Env file not found: {env_path}")

    if recipient_path is None:
        recipient_path = os.environ.get(
            "AGE_RECIPIENT_FILE",
            str(Path.home() / ".age" / "key.pub"),
        )

    recipient_path = Path(recipient_path)
    if not recipient_path.exists():
        raise FileNotFoundError(f"Age recipient file not found: {recipient_path}")

    # key.pub may contain "Public key: age1..." prefix; extract just the key
    pub_text = recipient_path.read_text().strip()
    if pub_text.startswith("Public key:"):
        pub_text = pub_text.split()[-1]
    recipient = pyrage.x25519.Recipient.from_str(pub_text)
    plaintext = env_path.read_bytes()
    ciphertext = pyrage.encrypt(plaintext, [recipient])

    if output_path is None:
        output_path = env_path.with_suffix(env_path.suffix + ".enc")
    else:
        output_path = Path(output_path)

    output_path.write_bytes(ciphertext)
    return output_path


def _parse_env(content: str) -> dict[str, str]:
    """Parse a .env file into key-value pairs.

    Handles comments, empty lines, and optional quoting.
    """
    env = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes (and unescape JSON string escapes for double-quoted values)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            if value[0] == '"':
                value = json.loads(value)
            else:
                value = value[1:-1]
        env[key] = value
    return env
