"""Tests for the PDF reader executor."""

from __future__ import annotations

import json
import os

import pytest

from executors.pdf_reader.executor import (
    _extract_passages,
    _parse_page_ranges,
    _safe_path,
    action_read,
    action_search,
    main,
    register_skill,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_env(**kwargs: str):
    """Set environment variables, returning a cleanup function."""
    old = {}
    for k, v in kwargs.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v

    def restore():
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return restore


def _create_test_pdf(path: str, pages: int = 3) -> str:
    """Create a simple test PDF with numbered pages."""
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        text_point = fitz.Point(72, 72)
        page.insert_text(text_point, f"Page {i + 1} content. This is test text on page {i + 1}.")
        if i == 1:
            page.insert_text(fitz.Point(72, 100), "Special keyword: creel-test-marker")
    doc.save(path)
    doc.close()
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path):
    """Create a workspace with a test PDF."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    old_ws = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(ws)
    yield ws
    if old_ws is None:
        os.environ.pop("WORKSPACE", None)
    else:
        os.environ["WORKSPACE"] = old_ws


@pytest.fixture()
def test_pdf(workspace):
    """Create a test PDF in the workspace."""
    pdf_path = workspace / "test.pdf"
    _create_test_pdf(str(pdf_path), pages=5)
    return pdf_path


# ---------------------------------------------------------------------------
# register_skill
# ---------------------------------------------------------------------------


class TestRegisterSkill:
    def test_returns_meta_and_execute(self):
        meta, execute = register_skill()
        assert meta.id == "pdf_reader"
        assert meta.label == "PDF Reader"
        assert callable(execute)
        assert len(meta.tools) == 2
        assert meta.needs_network is False

    def test_tool_names(self):
        meta, _ = register_skill()
        names = [t.name for t in meta.tools]
        assert names == ["read_pdf", "search_pdf"]

    def test_read_pdf_params(self):
        meta, _ = register_skill()
        read_tool = meta.tools[0]
        param_names = [p.name for p in read_tool.params]
        assert "file_path" in param_names
        assert "pages" in param_names

    def test_search_pdf_params(self):
        meta, _ = register_skill()
        search_tool = meta.tools[1]
        param_names = [p.name for p in search_tool.params]
        assert "file_path" in param_names
        assert "query" in param_names


# ---------------------------------------------------------------------------
# _safe_path
# ---------------------------------------------------------------------------


class TestSafePath:
    def test_valid_relative_path(self, workspace):
        result = _safe_path("test.pdf")
        assert result == str(workspace / "test.pdf")

    def test_traversal_blocked(self, workspace):
        with pytest.raises(ValueError, match="Path escapes workspace"):
            _safe_path("../../etc/passwd")

    def test_symlink_traversal_blocked(self, workspace):
        target = "/etc/passwd"
        link = workspace / "evil_link.pdf"
        try:
            os.symlink(target, str(link))
        except OSError:
            pytest.skip("Cannot create symlinks")
        with pytest.raises(ValueError, match="Path escapes workspace"):
            _safe_path("evil_link.pdf")


# ---------------------------------------------------------------------------
# _parse_page_ranges
# ---------------------------------------------------------------------------


class TestParsePageRanges:
    def test_all_pages(self):
        assert _parse_page_ranges("all", 5) == [0, 1, 2, 3, 4]

    def test_empty_string(self):
        assert _parse_page_ranges("", 5) == [0, 1, 2, 3, 4]

    def test_single_page(self):
        assert _parse_page_ranges("3", 5) == [2]

    def test_page_range(self):
        assert _parse_page_ranges("2-4", 5) == [1, 2, 3]

    def test_comma_separated(self):
        assert _parse_page_ranges("1,3,5", 5) == [0, 2, 4]

    def test_mixed(self):
        assert _parse_page_ranges("1, 3-5", 5) == [0, 2, 3, 4]

    def test_out_of_range_clipped(self):
        # Pages beyond total are silently ignored
        assert _parse_page_ranges("4-10", 5) == [3, 4]

    def test_invalid_range_raises(self):
        with pytest.raises(ValueError, match="Invalid page range"):
            _parse_page_ranges("5-2", 10)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid page"):
            _parse_page_ranges("abc", 10)


# ---------------------------------------------------------------------------
# action_read
# ---------------------------------------------------------------------------


class TestActionRead:
    def test_read_all_pages(self, test_pdf, workspace):
        restore = _set_env(FILE_PATH="test.pdf", PAGES="all", ACTION="read")
        try:
            result = action_read()
            assert "error" not in result
            assert result["total_pages"] == 5
            assert result["pages_read"] == 5
            assert len(result["pages"]) == 5
            assert "Page 1 content" in result["pages"][0]["text"]
        finally:
            restore()

    def test_read_specific_pages(self, test_pdf, workspace):
        restore = _set_env(FILE_PATH="test.pdf", PAGES="1,3", ACTION="read")
        try:
            result = action_read()
            assert result["pages_read"] == 2
            assert result["pages"][0]["page"] == 1
            assert result["pages"][1]["page"] == 3
        finally:
            restore()

    def test_read_page_range(self, test_pdf, workspace):
        restore = _set_env(FILE_PATH="test.pdf", PAGES="2-4", ACTION="read")
        try:
            result = action_read()
            assert result["pages_read"] == 3
        finally:
            restore()

    def test_missing_file_path(self, workspace):
        restore = _set_env(ACTION="read")
        os.environ.pop("FILE_PATH", None)
        try:
            result = action_read()
            assert "error" in result
            assert "FILE_PATH" in result["error"]
        finally:
            restore()

    def test_file_not_found(self, workspace):
        restore = _set_env(FILE_PATH="nonexistent.pdf", ACTION="read")
        try:
            result = action_read()
            assert "error" in result
            assert "not found" in result["error"].lower()
        finally:
            restore()

    def test_not_a_file(self, workspace):
        subdir = workspace / "subdir"
        subdir.mkdir()
        restore = _set_env(FILE_PATH="subdir", ACTION="read")
        try:
            result = action_read()
            assert "error" in result
            assert "Not a file" in result["error"]
        finally:
            restore()

    def test_invalid_page_range(self, test_pdf, workspace):
        restore = _set_env(FILE_PATH="test.pdf", PAGES="abc", ACTION="read")
        try:
            result = action_read()
            assert "error" in result
        finally:
            restore()


# ---------------------------------------------------------------------------
# action_search
# ---------------------------------------------------------------------------


class TestActionSearch:
    def test_search_found(self, test_pdf, workspace):
        restore = _set_env(FILE_PATH="test.pdf", QUERY="creel-test-marker", ACTION="search")
        try:
            result = action_search()
            assert "error" not in result
            assert result["matching_pages"] == 1
            assert result["matches"][0]["page"] == 2
            assert len(result["matches"][0]["passages"]) >= 1
        finally:
            restore()

    def test_search_not_found(self, test_pdf, workspace):
        restore = _set_env(FILE_PATH="test.pdf", QUERY="nonexistent-xyz-string", ACTION="search")
        try:
            result = action_search()
            assert result["matching_pages"] == 0
            assert result["matches"] == []
        finally:
            restore()

    def test_search_case_insensitive(self, test_pdf, workspace):
        restore = _set_env(FILE_PATH="test.pdf", QUERY="CREEL-TEST-MARKER", ACTION="search")
        try:
            result = action_search()
            assert result["matching_pages"] == 1
        finally:
            restore()

    def test_search_missing_query(self, test_pdf, workspace):
        restore = _set_env(FILE_PATH="test.pdf", ACTION="search")
        os.environ.pop("QUERY", None)
        try:
            result = action_search()
            assert "error" in result
            assert "QUERY" in result["error"]
        finally:
            restore()

    def test_search_missing_file(self, workspace):
        restore = _set_env(FILE_PATH="nope.pdf", QUERY="test", ACTION="search")
        try:
            result = action_search()
            assert "error" in result
        finally:
            restore()

    def test_search_multiple_pages(self, test_pdf, workspace):
        # "content" appears on every page
        restore = _set_env(FILE_PATH="test.pdf", QUERY="content", ACTION="search")
        try:
            result = action_search()
            assert result["matching_pages"] == 5
        finally:
            restore()


# ---------------------------------------------------------------------------
# _extract_passages
# ---------------------------------------------------------------------------


class TestExtractPassages:
    def test_basic_extraction(self):
        text = "Hello world this is a test of passage extraction in a document."
        passages = _extract_passages(text, "test")
        assert len(passages) == 1
        assert "test" in passages[0]

    def test_multiple_matches(self):
        text = "foo bar foo baz foo"
        passages = _extract_passages(text, "foo")
        assert len(passages) == 3

    def test_context_truncation(self):
        text = "A" * 200 + "NEEDLE" + "B" * 200
        passages = _extract_passages(text, "NEEDLE", context_chars=50)
        assert len(passages) == 1
        assert passages[0].startswith("...")
        assert passages[0].endswith("...")
        assert "NEEDLE" in passages[0]

    def test_max_passages_limit(self):
        text = " ".join(["match"] * 20)
        passages = _extract_passages(text, "match")
        assert len(passages) <= 10


# ---------------------------------------------------------------------------
# main (CLI entrypoint)
# ---------------------------------------------------------------------------


class TestMain:
    def test_unknown_action(self, capsys):
        restore = _set_env(ACTION="invalid")
        try:
            with pytest.raises(SystemExit, match="1"):
                main()
            captured = capsys.readouterr()
            assert "Unknown action" in captured.err
        finally:
            restore()

    def test_read_action_success(self, test_pdf, workspace, capsys):
        restore = _set_env(ACTION="read", FILE_PATH="test.pdf", PAGES="1")
        try:
            main()
            captured = capsys.readouterr()
            result = json.loads(captured.out)
            assert result["pages_read"] == 1
        finally:
            restore()

    def test_read_action_error(self, workspace, capsys):
        restore = _set_env(ACTION="read", FILE_PATH="missing.pdf")
        try:
            with pytest.raises(SystemExit, match="1"):
                main()
            captured = capsys.readouterr()
            err = json.loads(captured.err)
            assert "error" in err
        finally:
            restore()
