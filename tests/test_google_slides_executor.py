"""Tests for the Google Slides executor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from executors.google_slides.executor import (
    _extract_slide_text,
    add_slide,
    create_presentation,
    main,
    read_presentation,
    replace_text,
)


class TestExtractSlideText:
    def test_extracts_text_from_shapes(self) -> None:
        slide = {
            "pageElements": [
                {
                    "shape": {
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "Title\n"}},
                            ]
                        }
                    }
                },
                {
                    "shape": {
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "Body text\n"}},
                            ]
                        }
                    }
                },
            ]
        }
        assert _extract_slide_text(slide) == "Title\nBody text\n"

    def test_empty_slide(self) -> None:
        assert _extract_slide_text({}) == ""
        assert _extract_slide_text({"pageElements": []}) == ""

    def test_skips_non_shape_elements(self) -> None:
        slide = {
            "pageElements": [
                {"image": {}},
                {
                    "shape": {
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "text"}},
                            ]
                        }
                    }
                },
            ]
        }
        assert _extract_slide_text(slide) == "text"


class TestReadSlides:
    @patch("executors.google_slides.executor.get_credentials")
    @patch("executors.google_slides.executor.build")
    def test_read_extracts_slide_text(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.presentations().get().execute.return_value = {
            "presentationId": "pres-123",
            "title": "My Presentation",
            "slides": [
                {
                    "objectId": "slide1",
                    "pageElements": [
                        {
                            "shape": {
                                "text": {
                                    "textElements": [
                                        {"textRun": {"content": "Slide 1 text\n"}},
                                    ]
                                }
                            }
                        }
                    ],
                }
            ],
        }

        result = read_presentation("pres-123")

        assert result["presentationId"] == "pres-123"
        assert result["title"] == "My Presentation"
        assert result["slideCount"] == 1
        assert result["slides"][0]["text"] == "Slide 1 text\n"

    @patch("executors.google_slides.executor.get_credentials")
    @patch("executors.google_slides.executor.build")
    def test_read_returns_slide_count_and_titles(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.presentations().get().execute.return_value = {
            "presentationId": "pres-123",
            "title": "Multi-slide",
            "slides": [
                {"objectId": "s1", "pageElements": []},
                {"objectId": "s2", "pageElements": []},
                {"objectId": "s3", "pageElements": []},
            ],
        }

        result = read_presentation("pres-123")

        assert result["slideCount"] == 3
        assert len(result["slides"]) == 3
        assert result["slides"][0]["slideNumber"] == 1
        assert result["slides"][2]["slideNumber"] == 3


class TestCreateSlides:
    @patch("executors.google_slides.executor.get_credentials")
    @patch("executors.google_slides.executor.build")
    def test_create_with_title(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.presentations().create().execute.return_value = {
            "presentationId": "new-pres",
        }

        result = create_presentation("My Slides")

        assert result["presentationId"] == "new-pres"
        assert "new-pres" in result["url"]


class TestAddSlide:
    @patch("executors.google_slides.executor.get_credentials")
    @patch("executors.google_slides.executor.build")
    def test_add_slide_returns_slide_id(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.presentations().batchUpdate().execute.return_value = {
            "replies": [{"createSlide": {"objectId": "new-slide-1"}}],
        }
        # Mock the get call for finding placeholders
        mock_service.presentations().get().execute.return_value = {
            "presentationId": "pres-123",
            "slides": [
                {"objectId": "new-slide-1", "pageElements": []},
            ],
        }

        result = add_slide("pres-123")

        assert result["slideId"] == "new-slide-1"
        assert result["presentationId"] == "pres-123"

    @patch("executors.google_slides.executor.get_credentials")
    @patch("executors.google_slides.executor.build")
    def test_add_slide_default_layout(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.presentations().batchUpdate().execute.return_value = {
            "replies": [{"createSlide": {"objectId": "slide-2"}}],
        }
        mock_service.presentations().get().execute.return_value = {
            "presentationId": "pres-123",
            "slides": [{"objectId": "slide-2", "pageElements": []}],
        }

        add_slide("pres-123")

        # Verify batchUpdate was called (with BLANK layout by default)
        mock_service.presentations().batchUpdate.assert_called()


class TestReplaceInSlides:
    @patch("executors.google_slides.executor.get_credentials")
    @patch("executors.google_slides.executor.build")
    def test_replace_text(self, mock_build: MagicMock, mock_creds: MagicMock) -> None:
        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_service.presentations().batchUpdate().execute.return_value = {
            "replies": [{"replaceAllText": {"occurrencesChanged": 5}}],
        }

        result = replace_text("pres-123", "old", "new")

        assert result["occurrencesChanged"] == 5
        assert result["presentationId"] == "pres-123"


class TestMainEntrypoint:
    @patch("executors.google_slides.executor.read_presentation")
    def test_main_dispatches_read_action(self, mock_read: MagicMock, monkeypatch) -> None:
        monkeypatch.setenv("ACTION", "read")
        monkeypatch.setenv("PRESENTATION_ID", "pres-1")
        mock_read.return_value = {
            "presentationId": "pres-1",
            "title": "T",
            "slideCount": 0,
            "slides": [],
        }

        with patch("builtins.print"):
            main()
        mock_read.assert_called_once_with("pres-1")

    @patch("executors.google_slides.executor.create_presentation")
    def test_main_dispatches_create_action(self, mock_create: MagicMock, monkeypatch) -> None:
        monkeypatch.setenv("ACTION", "create")
        monkeypatch.setenv("TITLE", "New Pres")
        mock_create.return_value = {"presentationId": "new", "url": "http://x"}

        with patch("builtins.print"):
            main()
        mock_create.assert_called_once_with("New Pres")

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
