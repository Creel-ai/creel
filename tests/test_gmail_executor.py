"""Tests for the Gmail executors (readonly and modify)."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from executors.gmail_modify.executor import (
    _MAX_BATCH_SIZE,
    _parse_ids,
    batch_delete,
    batch_modify,
    batch_trash,
)
from executors.gmail_readonly.executor import (
    _clean_snippet,
    _extract_body,
    _extract_headers,
    fetch_emails,
)


class TestExtractBody:
    """Tests for _extract_body MIME body extraction."""

    def test_simple_text_plain(self) -> None:
        payload = {
            "mimeType": "text/plain",
            "body": {
                "data": base64.urlsafe_b64encode(b"Hello world").decode(),
            },
        }
        assert _extract_body(payload) == "Hello world"

    def test_simple_text_html_stripped(self) -> None:
        html = "<html><body><p>Hello <b>world</b></p></body></html>"
        payload = {
            "mimeType": "text/html",
            "body": {
                "data": base64.urlsafe_b64encode(html.encode()).decode(),
            },
        }
        result = _extract_body(payload)
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_multipart_alternative_prefers_plain(self) -> None:
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {
                        "data": base64.urlsafe_b64encode(b"Plain text").decode(),
                    },
                },
                {
                    "mimeType": "text/html",
                    "body": {
                        "data": base64.urlsafe_b64encode(b"<p>HTML text</p>").decode(),
                    },
                },
            ],
        }
        assert _extract_body(payload) == "Plain text"

    def test_multipart_alternative_html_fallback(self) -> None:
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {
                        "data": base64.urlsafe_b64encode(b"<p>Only HTML</p>").decode(),
                    },
                },
            ],
        }
        result = _extract_body(payload)
        assert "Only HTML" in result
        assert "<" not in result

    def test_nested_multipart_mixed(self) -> None:
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {
                                "data": base64.urlsafe_b64encode(b"Nested plain").decode(),
                            },
                        },
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "some-id"},
                },
            ],
        }
        assert _extract_body(payload) == "Nested plain"

    def test_empty_body(self) -> None:
        payload = {"mimeType": "text/plain", "body": {}}
        assert _extract_body(payload) == ""

    def test_empty_parts(self) -> None:
        payload = {"mimeType": "multipart/mixed", "parts": []}
        assert _extract_body(payload) == ""

    def test_no_parts_no_body(self) -> None:
        payload = {"mimeType": "multipart/mixed"}
        assert _extract_body(payload) == ""

    def test_base64url_decoding(self) -> None:
        # base64url uses - and _ instead of + and /
        text = "Hello+World/Test"
        encoded = base64.urlsafe_b64encode(text.encode()).decode()
        payload = {
            "mimeType": "text/plain",
            "body": {"data": encoded},
        }
        assert _extract_body(payload) == text


class TestCleanSnippet:
    """Tests for _clean_snippet sanitization."""

    def test_decodes_html_entities(self) -> None:
        assert _clean_snippet("F1&#39;s Super Bowl") == "F1's Super Bowl"

    def test_strips_zero_width_chars(self) -> None:
        assert _clean_snippet("Hello \u034f\u200c\u034f\u200c world") == "Hello  world"

    def test_combined(self) -> None:
        raw = "F1&#39;s race \u034f\u200c \u034f\u200c \u034f\u200c"
        assert _clean_snippet(raw) == "F1's race"

    def test_passthrough_clean_text(self) -> None:
        assert _clean_snippet("Normal snippet") == "Normal snippet"


class TestExtractHeaders:
    """Tests for _extract_headers."""

    def test_extracts_requested_headers(self) -> None:
        headers = [
            {"name": "Subject", "value": "Test email"},
            {"name": "From", "value": "alice@example.com"},
            {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            {"name": "To", "value": "bob@example.com"},
        ]
        result = _extract_headers(headers, ["Subject", "From"])
        assert result == {
            "Subject": "Test email",
            "From": "alice@example.com",
        }

    def test_case_insensitive_matching(self) -> None:
        headers = [
            {"name": "subject", "value": "Lowercase subject"},
            {"name": "FROM", "value": "uppercase@example.com"},
        ]
        result = _extract_headers(headers, ["Subject", "From"])
        assert result["Subject"] == "Lowercase subject"
        assert result["From"] == "uppercase@example.com"

    def test_missing_headers_not_included(self) -> None:
        headers = [
            {"name": "Subject", "value": "Test"},
        ]
        result = _extract_headers(headers, ["Subject", "From", "Date"])
        assert result == {"Subject": "Test"}
        assert "From" not in result
        assert "Date" not in result

    def test_empty_headers(self) -> None:
        result = _extract_headers([], ["Subject"])
        assert result == {}


class TestFetchEmails:
    """Tests for fetch_emails with mocked API."""

    @patch("executors.gmail_readonly.executor.get_credentials")
    @patch("executors.gmail_readonly.executor.build")
    def test_fetch_emails_metadata_mode(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()

        # Set up mock service
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_list = mock_service.users().messages().list
        mock_list.return_value.execute.return_value = {
            "messages": [{"id": "msg1"}],
        }

        mock_get = mock_service.users().messages().get
        mock_get.return_value.execute.return_value = {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Test Subject"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Date", "value": "2024-01-01"},
                ],
            },
            "snippet": "Email preview...",
            "labelIds": ["INBOX", "UNREAD"],
        }

        emails = fetch_emails(query="is:unread", max_results=5, full_body=False)

        assert len(emails) == 1
        assert emails[0]["subject"] == "Test Subject"
        assert emails[0]["from"] == "sender@example.com"
        assert emails[0]["to"] == "me@example.com"
        assert emails[0]["snippet"] == "Email preview..."
        assert "UNREAD" in emails[0]["labels"]
        assert "body" not in emails[0]

        # Verify API was called with correct params
        mock_list.assert_called_with(userId="me", q="is:unread", maxResults=5)
        mock_get.assert_called_with(
            userId="me",
            id="msg1",
            format="metadata",
            metadataHeaders=["Subject", "From", "Date", "To"],
        )

    @patch("executors.gmail_readonly.executor.get_credentials")
    @patch("executors.gmail_readonly.executor.build")
    def test_fetch_emails_full_body_mode(
        self, mock_build: MagicMock, mock_creds: MagicMock
    ) -> None:
        mock_creds.return_value = MagicMock()

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_list = mock_service.users().messages().list
        mock_list.return_value.execute.return_value = {
            "messages": [{"id": "msg1"}],
        }

        body_data = base64.urlsafe_b64encode(b"Email body text").decode()
        mock_get = mock_service.users().messages().get
        mock_get.return_value.execute.return_value = {
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Full Body Test"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Date", "value": "2024-01-01"},
                ],
                "body": {"data": body_data},
            },
            "snippet": "Email body...",
            "labelIds": ["INBOX"],
        }

        emails = fetch_emails(query="is:unread", max_results=5, full_body=True)

        assert len(emails) == 1
        assert emails[0]["body"] == "Email body text"
        assert emails[0]["subject"] == "Full Body Test"

        # Verify full format was requested
        mock_get.assert_called_with(userId="me", id="msg1", format="full")

    @patch("executors.gmail_readonly.executor.get_credentials")
    @patch("executors.gmail_readonly.executor.build")
    def test_fetch_emails_empty_results(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_list = mock_service.users().messages().list
        mock_list.return_value.execute.return_value = {}

        emails = fetch_emails()
        assert emails == []


# ---------------------------------------------------------------------------
# Gmail modify executor — batch operations
# ---------------------------------------------------------------------------


class TestParseIds:
    def test_comma_separated(self) -> None:
        assert _parse_ids("a,b,c") == ["a", "b", "c"]

    def test_strips_whitespace(self) -> None:
        assert _parse_ids(" a , b , c ") == ["a", "b", "c"]

    def test_empty_string(self) -> None:
        assert _parse_ids("") == []

    def test_trailing_comma(self) -> None:
        assert _parse_ids("a,b,") == ["a", "b"]


class TestBatchModify:
    @patch("executors.gmail_modify.executor.get_credentials")
    @patch("executors.gmail_modify.executor.build")
    def test_batch_modify_calls_api(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        result = batch_modify(["m1", "m2"], add_labels=["STARRED"], remove_labels=["UNREAD"])

        assert result == {"modified": 2, "ids": ["m1", "m2"]}
        mock_service.users().messages().batchModify.assert_called_once_with(
            userId="me",
            body={"ids": ["m1", "m2"], "addLabelIds": ["STARRED"], "removeLabelIds": ["UNREAD"]},
        )

    def test_batch_modify_rejects_oversized_list(self) -> None:
        ids = [f"m{i}" for i in range(_MAX_BATCH_SIZE + 1)]
        with pytest.raises(ValueError, match="Too many message IDs"):
            batch_modify(ids)


class TestBatchTrash:
    @patch("executors.gmail_modify.executor.get_credentials")
    @patch("executors.gmail_modify.executor.build")
    def test_batch_trash_delegates_to_modify(
        self, mock_build: MagicMock, mock_creds: MagicMock
    ) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        result = batch_trash(["m1"])

        assert result == {"modified": 1, "ids": ["m1"]}
        mock_service.users().messages().batchModify.assert_called_once_with(
            userId="me",
            body={"ids": ["m1"], "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]},
        )


class TestBatchDelete:
    @patch("executors.gmail_modify.executor.get_credentials")
    @patch("executors.gmail_modify.executor.build")
    def test_batch_delete_calls_api(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        result = batch_delete(["m1", "m2"])

        assert result == {"deleted": 2, "ids": ["m1", "m2"]}
        mock_service.users().messages().batchDelete.assert_called_once_with(
            userId="me",
            body={"ids": ["m1", "m2"]},
        )

    def test_batch_delete_rejects_oversized_list(self) -> None:
        ids = [f"m{i}" for i in range(_MAX_BATCH_SIZE + 1)]
        with pytest.raises(ValueError, match="Too many message IDs"):
            batch_delete(ids)
