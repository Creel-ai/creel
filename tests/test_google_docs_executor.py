"""Tests for the Google Docs executor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from executors.google_docs.executor import (
    _extract_text,
    append_text,
    create_document,
    insert_text,
    main,
    read_document,
    replace_text,
)


class TestExtractText:
    def test_extracts_plain_text(self) -> None:
        body = {
            "content": [
                {
                    "paragraph": {
                        "elements": [
                            {"textRun": {"content": "Hello "}},
                            {"textRun": {"content": "world\n"}},
                        ]
                    }
                },
                {
                    "paragraph": {
                        "elements": [
                            {"textRun": {"content": "Second paragraph\n"}},
                        ]
                    }
                },
            ]
        }
        assert _extract_text(body) == "Hello world\nSecond paragraph\n"

    def test_empty_body(self) -> None:
        assert _extract_text({}) == ""
        assert _extract_text({"content": []}) == ""

    def test_skips_non_paragraph_elements(self) -> None:
        body = {
            "content": [
                {"sectionBreak": {}},
                {"paragraph": {"elements": [{"textRun": {"content": "text"}}]}},
            ]
        }
        assert _extract_text(body) == "text"


class TestReadDoc:
    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_read_extracts_plain_text(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.documents().get().execute.return_value = {
            "documentId": "doc-123",
            "title": "My Doc",
            "body": {
                "content": [
                    {"paragraph": {"elements": [{"textRun": {"content": "Hello world\n"}}]}}
                ]
            },
        }

        result = read_document("doc-123")

        assert result["documentId"] == "doc-123"
        assert result["title"] == "My Doc"
        assert result["content"] == "Hello world\n"

    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_read_empty_document(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.documents().get().execute.return_value = {
            "documentId": "doc-empty",
            "title": "Empty",
            "body": {"content": []},
        }

        result = read_document("doc-empty")

        assert result["content"] == ""


class TestCreateDoc:
    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_create_with_title_only(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.documents().create().execute.return_value = {
            "documentId": "new-doc",
        }

        result = create_document("My New Doc")

        assert result["documentId"] == "new-doc"
        mock_service.documents().create.assert_called()
        # No batchUpdate since no body
        mock_service.documents().batchUpdate.assert_not_called()

    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_create_with_body(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.documents().create().execute.return_value = {
            "documentId": "new-doc",
        }

        create_document("My Doc", body="Initial content")

        mock_service.documents().batchUpdate.assert_called_once()

    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_create_returns_document_id_and_url(
        self, mock_build: MagicMock, mock_creds: MagicMock
    ) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.documents().create().execute.return_value = {
            "documentId": "abc123",
        }

        result = create_document("Test")

        assert result["documentId"] == "abc123"
        assert "abc123" in result["url"]


class TestAppendToDoc:
    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_append_text(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        result = append_text("doc-123", "New text at end")

        assert result["documentId"] == "doc-123"
        assert result["appended"] is True
        mock_service.documents().batchUpdate.assert_called_once()


class TestReplaceInDoc:
    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_replace_text(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.documents().batchUpdate().execute.return_value = {
            "replies": [{"replaceAllText": {"occurrencesChanged": 3}}],
        }

        result = replace_text("doc-123", "old", "new")

        assert result["occurrencesChanged"] == 3

    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_replace_with_match_case(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.documents().batchUpdate().execute.return_value = {
            "replies": [{"replaceAllText": {"occurrencesChanged": 1}}],
        }

        result = replace_text("doc-123", "Hello", "Hi", match_case=False)

        assert result["occurrencesChanged"] == 1

    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_replace_returns_occurrence_count(
        self, mock_build: MagicMock, mock_creds: MagicMock
    ) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.documents().batchUpdate().execute.return_value = {
            "replies": [{"replaceAllText": {"occurrencesChanged": 0}}],
        }

        result = replace_text("doc-123", "nonexistent", "x")

        assert result["occurrencesChanged"] == 0


class TestInsertInDoc:
    @patch("executors.google_docs.executor.get_credentials")
    @patch("executors.google_docs.executor.build")
    def test_insert_at_index(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        result = insert_text("doc-123", "Inserted text", 5)

        assert result["documentId"] == "doc-123"
        assert result["inserted"] is True
        mock_service.documents().batchUpdate.assert_called_once()


class TestMainEntrypoint:
    @patch("executors.google_docs.executor.read_document")
    def test_main_dispatches_read_action(self, mock_read: MagicMock, monkeypatch) -> None:
        monkeypatch.setenv("ACTION", "read")
        monkeypatch.setenv("DOCUMENT_ID", "doc-1")
        mock_read.return_value = {"documentId": "doc-1", "title": "T", "content": "c"}

        with patch("builtins.print"):
            main()
        mock_read.assert_called_once_with("doc-1")

    @patch("executors.google_docs.executor.create_document")
    def test_main_dispatches_create_action(self, mock_create: MagicMock, monkeypatch) -> None:
        monkeypatch.setenv("ACTION", "create")
        monkeypatch.setenv("TITLE", "New Doc")
        mock_create.return_value = {"documentId": "new", "url": "http://x"}

        with patch("builtins.print"):
            main()
        mock_create.assert_called_once_with("New Doc", "")

    def test_main_unknown_action_exits_1(self, monkeypatch) -> None:
        monkeypatch.setenv("ACTION", "unknown")
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_main_missing_action_exits_1(self, monkeypatch) -> None:
        monkeypatch.delenv("ACTION", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
