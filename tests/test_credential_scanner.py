"""Tests for the post-execution credential scanner."""

from __future__ import annotations

from guardian.credential_scanner import (
    _redact,
    redact_credentials,
    scan_for_credentials,
)


class TestScanForCredentials:
    """Tests for credential pattern detection."""

    def test_empty_text(self) -> None:
        assert scan_for_credentials("") == []

    def test_no_credentials(self) -> None:
        assert scan_for_credentials("Hello, this is normal text.") == []

    def test_detect_aws_access_key(self) -> None:
        text = "key=AKIAIOSFODNN7EXAMPLE"
        matches = scan_for_credentials(text)
        assert len(matches) == 1
        assert matches[0].pattern_name == "aws_access_key"

    def test_detect_google_api_key(self) -> None:
        text = "GOOGLE_KEY=AIzaSyD-1234567890abcdefghijklmnopqrstuv"
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "google_api_key" for m in matches)

    def test_detect_github_token(self) -> None:
        text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "github_token" for m in matches)

    def test_detect_github_fine_grained(self) -> None:
        text = "auth: github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "github_fine_grained" for m in matches)

    def test_detect_anthropic_key(self) -> None:
        text = (
            "ANTHROPIC_API_KEY=sk-ant-abcdef1234567890abcdef1234567890abcdef1234567890"
        )
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "anthropic_api_key" for m in matches)

    def test_detect_openai_key(self) -> None:
        text = "OPENAI_KEY=sk-1234567890abcdefghijklmn"
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "openai_api_key" for m in matches)

    def test_detect_slack_bot_token(self) -> None:
        # Build token dynamically to avoid GitHub push protection
        prefix = "xoxb-"
        text = f"SLACK_TOKEN={prefix}1234567890123-ABCDEFGHIJKLMNOPQRST0123456789ab"
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "slack_bot_token" for m in matches)

    def test_detect_stripe_key(self) -> None:
        # Build key dynamically to avoid GitHub push protection
        prefix = "sk_" + "live_"
        text = f"STRIPE_KEY={prefix}00112233445566778899aabb"
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "stripe_secret_key" for m in matches)

    def test_detect_private_key(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAI..."
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "private_key" for m in matches)

    def test_detect_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc"
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "bearer_token" for m in matches)

    def test_detect_generic_api_key(self) -> None:
        text = 'api_key = "abcdef1234567890abcdef1234567890"'
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "generic_api_key" for m in matches)

    def test_detect_generic_password(self) -> None:
        text = 'password = "my_super_secret_password_123"'
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "generic_password" for m in matches)

    def test_detect_google_oauth_token(self) -> None:
        text = "access_token: ya29.a0AfB_byDxxxxxxxxxxxxxxxxxxxxxxxxx"
        matches = scan_for_credentials(text)
        assert any(m.pattern_name == "google_oauth_token" for m in matches)

    def test_multiple_credentials_in_one_text(self) -> None:
        text = "AWS: AKIAIOSFODNN7EXAMPLE\nGitHub: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij\n"
        matches = scan_for_credentials(text)
        pattern_names = {m.pattern_name for m in matches}
        assert "aws_access_key" in pattern_names
        assert "github_token" in pattern_names


class TestRedactCredentials:
    """Tests for credential redaction."""

    def test_no_credentials_unchanged(self) -> None:
        text = "Normal text with no secrets."
        redacted, matches = redact_credentials(text)
        assert redacted == text
        assert matches == []

    def test_aws_key_redacted(self) -> None:
        text = "key=AKIAIOSFODNN7EXAMPLE here"
        redacted, matches = redact_credentials(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert "[REDACTED:aws_access_key]" in redacted
        assert len(matches) == 1

    def test_github_token_redacted(self) -> None:
        text = "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij end"
        redacted, matches = redact_credentials(text)
        assert "ghp_ABCDEF" not in redacted
        assert "[REDACTED:github_token]" in redacted

    def test_multiple_credentials_all_redacted(self) -> None:
        text = "AWS: AKIAIOSFODNN7EXAMPLE GH: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        redacted, matches = redact_credentials(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert "ghp_ABCDEF" not in redacted
        assert len(matches) >= 2

    def test_empty_text(self) -> None:
        redacted, matches = redact_credentials("")
        assert redacted == ""
        assert matches == []


class TestRedactHelper:
    """Tests for the _redact helper function."""

    def test_short_text(self) -> None:
        assert _redact("abcdef") == "ab***"

    def test_long_text(self) -> None:
        result = _redact("AKIAIOSFODNN7EXAMPLE")
        assert result.startswith("AKIA")
        assert result.endswith("LE")
        assert "..." in result

    def test_very_short_text(self) -> None:
        assert _redact("ab") == "ab***"
