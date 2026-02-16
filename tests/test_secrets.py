"""Tests for secrets management."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskrunner.secrets import (
    _parse_env,
    decrypt_env_file,
    encrypt_env_file,
    parse_env_file,
)


# ---------------------------------------------------------------------------
# _parse_env tests (existing)
# ---------------------------------------------------------------------------


def test_parse_env_basic() -> None:
    content = "KEY=value\nANOTHER=thing"
    result = _parse_env(content)
    assert result == {"KEY": "value", "ANOTHER": "thing"}


def test_parse_env_quoted_values() -> None:
    content = 'KEY="quoted value"\nSINGLE=\'single quoted\''
    result = _parse_env(content)
    assert result == {"KEY": "quoted value", "SINGLE": "single quoted"}


def test_parse_env_comments_and_blanks() -> None:
    content = "# comment\n\nKEY=value\n  # another comment\nKEY2=val2"
    result = _parse_env(content)
    assert result == {"KEY": "value", "KEY2": "val2"}


def test_parse_env_equals_in_value() -> None:
    content = "KEY=value=with=equals"
    result = _parse_env(content)
    assert result == {"KEY": "value=with=equals"}


def test_parse_env_empty() -> None:
    assert _parse_env("") == {}
    assert _parse_env("# just a comment") == {}


def test_parse_env_whitespace_handling() -> None:
    content = "  KEY  =  value  "
    result = _parse_env(content)
    assert result == {"KEY": "value"}


def test_parse_env_json_escaped_double_quotes() -> None:
    """Double-quoted values with JSON escapes (as produced by setup-google-oauth.py)."""
    import json

    inner = json.dumps({"refresh_token": "tok", "client_id": "cid", "client_secret": "cs"})
    content = f"GOOGLE_CREDENTIALS_JSON={json.dumps(inner)}"
    result = _parse_env(content)
    assert json.loads(result["GOOGLE_CREDENTIALS_JSON"]) == {
        "refresh_token": "tok",
        "client_id": "cid",
        "client_secret": "cs",
    }


# ---------------------------------------------------------------------------
# encrypt / decrypt round-trip tests
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip(tmp_path: Path, age_keypair) -> None:
    """Encrypt a .env file, then decrypt it and verify contents match."""
    key_file, pub_file = age_keypair

    env_file = tmp_path / "secrets.env"
    env_file.write_text("API_KEY=sk-test-123\nSECRET=hello world\n")

    enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))
    assert enc_path.exists()
    assert enc_path.suffix == ".enc"

    result = decrypt_env_file(enc_path, identity_path=str(key_file))
    assert result == {"API_KEY": "sk-test-123", "SECRET": "hello world"}


def test_encrypt_custom_output_path(tmp_path: Path, age_keypair) -> None:
    key_file, pub_file = age_keypair

    env_file = tmp_path / "secrets.env"
    env_file.write_text("KEY=val\n")

    custom_out = tmp_path / "custom.enc"
    enc_path = encrypt_env_file(
        env_file, recipient_path=str(pub_file), output_path=custom_out
    )
    assert enc_path == custom_out
    assert enc_path.exists()


def test_encrypt_with_public_key_prefix(tmp_path: Path, age_keypair) -> None:
    """The 'Public key: age1...' prefix should be auto-stripped."""
    key_file, pub_file = age_keypair

    # Rewrite pub_file with "Public key:" prefix
    raw_key = pub_file.read_text().strip()
    pub_file.write_text(f"Public key: {raw_key}\n")

    env_file = tmp_path / "secrets.env"
    env_file.write_text("A=1\n")

    enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))
    result = decrypt_env_file(enc_path, identity_path=str(key_file))
    assert result == {"A": "1"}


def test_decrypt_missing_enc_file(tmp_path: Path, age_keypair) -> None:
    key_file, _ = age_keypair
    with pytest.raises(FileNotFoundError, match="Encrypted file not found"):
        decrypt_env_file(tmp_path / "missing.enc", identity_path=str(key_file))


def test_decrypt_missing_identity(tmp_path: Path) -> None:
    enc_file = tmp_path / "test.enc"
    enc_file.write_text("dummy")
    with pytest.raises(FileNotFoundError, match="Age identity file not found"):
        decrypt_env_file(enc_file, identity_path=str(tmp_path / "nokey.txt"))


def test_encrypt_missing_env_file(tmp_path: Path, age_keypair) -> None:
    _, pub_file = age_keypair
    with pytest.raises(FileNotFoundError, match="Env file not found"):
        encrypt_env_file(tmp_path / "missing.env", recipient_path=str(pub_file))


def test_encrypt_missing_recipient(tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text("K=V\n")
    with pytest.raises(FileNotFoundError, match="Age recipient file not found"):
        encrypt_env_file(env_file, recipient_path=str(tmp_path / "nopub.txt"))


def test_decrypt_env_var_override_identity(tmp_path: Path, age_keypair, monkeypatch) -> None:
    key_file, pub_file = age_keypair
    monkeypatch.setenv("AGE_IDENTITY_FILE", str(key_file))

    env_file = tmp_path / "secrets.env"
    env_file.write_text("X=42\n")
    enc_path = encrypt_env_file(env_file, recipient_path=str(pub_file))

    # identity_path=None triggers env var lookup
    result = decrypt_env_file(enc_path)
    assert result == {"X": "42"}


def test_encrypt_env_var_override_recipient(tmp_path: Path, age_keypair, monkeypatch) -> None:
    key_file, pub_file = age_keypair
    monkeypatch.setenv("AGE_RECIPIENT_FILE", str(pub_file))

    env_file = tmp_path / "secrets.env"
    env_file.write_text("Y=99\n")

    # recipient_path=None triggers env var lookup
    enc_path = encrypt_env_file(env_file)
    result = decrypt_env_file(enc_path, identity_path=str(key_file))
    assert result == {"Y": "99"}


# ---------------------------------------------------------------------------
# parse_env_file tests
# ---------------------------------------------------------------------------


def test_parse_env_file_from_disk(tmp_path: Path) -> None:
    f = tmp_path / "test.env"
    f.write_text("A=hello\nB=world\n")
    result = parse_env_file(f)
    assert result == {"A": "hello", "B": "world"}


def test_parse_env_file_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_env_file(tmp_path / "missing.env")
