"""Tests for the Google Sheets executor."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from executors.google_sheets.executor import (
    append_to_sheet,
    create_spreadsheet,
    main,
    read_sheet,
    write_to_sheet,
)


class TestReadSheet:
    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_read_returns_values(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().values().get().execute.return_value = {
            "range": "Sheet1!A1:B2",
            "values": [["Name", "Age"], ["Alice", "30"]],
        }

        result = read_sheet("spreadsheet-123", "Sheet1!A1:B2")

        assert result["range"] == "Sheet1!A1:B2"
        assert result["values"] == [["Name", "Age"], ["Alice", "30"]]

    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_read_empty_range(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().values().get().execute.return_value = {
            "range": "Sheet1!A1:A1",
        }

        result = read_sheet("spreadsheet-123", "Sheet1!A1:A1")

        assert result["values"] == []


class TestCreateSheet:
    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_create_with_title(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "new-id",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/new-id",
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }

        result = create_spreadsheet("My Sheet")

        assert result["spreadsheetId"] == "new-id"
        assert "new-id" in result["url"]

    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_create_with_initial_data(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "new-id",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/new-id",
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }

        data = json.dumps([["A", "B"], ["1", "2"]])
        create_spreadsheet("My Sheet", data=data)

        mock_service.spreadsheets().values().update.assert_called_once()

    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_create_returns_spreadsheet_id_and_url(
        self,
        mock_build: MagicMock,
        mock_creds: MagicMock,
    ) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "abc123",
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }

        result = create_spreadsheet("Test")

        assert result["spreadsheetId"] == "abc123"
        assert "abc123" in result["url"]


class TestWriteToSheet:
    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_write_values(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "Sheet1!A1:B2",
            "updatedRows": 2,
            "updatedColumns": 2,
            "updatedCells": 4,
        }

        data = json.dumps([["Name", "Age"], ["Alice", "30"]])
        result = write_to_sheet("spreadsheet-123", "Sheet1!A1:B2", data)

        assert result["updatedRange"] == "Sheet1!A1:B2"
        assert result["updatedCells"] == 4

    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_write_with_raw_input_option(
        self, mock_build: MagicMock, mock_creds: MagicMock
    ) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "Sheet1!A1",
            "updatedRows": 1,
            "updatedColumns": 1,
            "updatedCells": 1,
        }

        data = json.dumps([["=SUM(A1:A10)"]])
        write_to_sheet("spreadsheet-123", "Sheet1!A1", data, "RAW")

        call_kwargs = mock_service.spreadsheets().values().update.call_args
        assert call_kwargs is not None

    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_write_returns_updated_range(
        self, mock_build: MagicMock, mock_creds: MagicMock
    ) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "Sheet1!A1:C3",
            "updatedRows": 3,
            "updatedColumns": 3,
            "updatedCells": 9,
        }

        data = json.dumps([["a", "b", "c"]] * 3)
        result = write_to_sheet("spreadsheet-123", "Sheet1!A1:C3", data)

        assert result["updatedRange"] == "Sheet1!A1:C3"


class TestAppendToSheet:
    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_append_rows(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().values().append().execute.return_value = {
            "updates": {
                "updatedRange": "Sheet1!A3:B3",
                "updatedRows": 1,
                "updatedColumns": 2,
                "updatedCells": 2,
            }
        }

        data = json.dumps([["Bob", "25"]])
        result = append_to_sheet("spreadsheet-123", "Sheet1!A:B", data)

        assert result["updatedRange"] == "Sheet1!A3:B3"
        assert result["updatedCells"] == 2

    @patch("executors.google_sheets.executor.get_credentials")
    @patch("executors.google_sheets.executor.build")
    def test_append_returns_updated_range(
        self, mock_build: MagicMock, mock_creds: MagicMock
    ) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.spreadsheets().values().append().execute.return_value = {
            "updates": {
                "updatedRange": "Sheet1!A5:B5",
                "updatedRows": 1,
                "updatedColumns": 2,
                "updatedCells": 2,
            }
        }

        data = json.dumps([["Charlie", "35"]])
        result = append_to_sheet("spreadsheet-123", "Sheet1!A:B", data)

        assert result["updatedRange"] == "Sheet1!A5:B5"


class TestMainEntrypoint:
    @patch("executors.google_sheets.executor.read_sheet")
    def test_main_dispatches_read_action(self, mock_read: MagicMock, monkeypatch) -> None:
        monkeypatch.setenv("ACTION", "read")
        monkeypatch.setenv("SPREADSHEET_ID", "abc")
        monkeypatch.setenv("RANGE", "Sheet1!A1")
        mock_read.return_value = {"range": "Sheet1!A1", "values": [["x"]]}

        with patch("builtins.print") as mock_print:
            main()
        mock_read.assert_called_once_with("abc", "Sheet1!A1")
        mock_print.assert_called_once()

    @patch("executors.google_sheets.executor.create_spreadsheet")
    def test_main_dispatches_create_action(self, mock_create: MagicMock, monkeypatch) -> None:
        monkeypatch.setenv("ACTION", "create")
        monkeypatch.setenv("TITLE", "New Sheet")
        mock_create.return_value = {"spreadsheetId": "new", "url": "http://x"}

        with patch("builtins.print"):
            main()
        mock_create.assert_called_once_with("New Sheet", "", "")

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
