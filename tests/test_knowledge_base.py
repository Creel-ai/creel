"""Tests for the knowledge base module."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from creel.knowledge_base import (
    KnowledgeBase,
    _cosine_similarity,
    _decode_embedding,
    _encode_embedding,
    _is_supported_file,
    _split_by_headings,
    _split_by_paragraphs,
    _split_code,
    _split_with_overlap,
    chunk_text,
)

# ---------------------------------------------------------------------------
# Chunking tests
# ---------------------------------------------------------------------------


class TestChunking:
    def test_chunk_text_empty(self):
        assert chunk_text("", "test.md") == []
        assert chunk_text("   ", "test.md") == []

    def test_chunk_text_simple_markdown(self):
        text = "# Title\n\nFirst paragraph.\n\nSecond paragraph."
        chunks = chunk_text(text, "test.md", chunk_size=1000)
        assert len(chunks) >= 1
        assert all("content" in c for c in chunks)
        assert all("source" in c for c in chunks)
        assert chunks[0]["source"] == "test.md"

    def test_chunk_text_preserves_content(self):
        text = "Hello world, this is a test document."
        chunks = chunk_text(text, "test.txt", chunk_size=1000)
        assert len(chunks) == 1
        assert chunks[0]["content"] == text

    def test_chunk_text_splits_large_documents(self):
        # Create a document larger than chunk_size
        paragraphs = [f"Paragraph {i}. " * 20 for i in range(10)]
        text = "\n\n".join(paragraphs)
        chunks = chunk_text(text, "test.txt", chunk_size=200, chunk_overlap=20)
        assert len(chunks) > 1

    def test_chunk_text_code_file(self):
        text = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        chunks = chunk_text(text, "test.py", chunk_size=1000)
        assert len(chunks) >= 1

    def test_split_by_headings(self):
        text = "# Title\n\nIntro text.\n\n## Section 1\n\nContent 1.\n\n## Section 2\n\nContent 2."
        sections = _split_by_headings(text)
        assert len(sections) >= 2

    def test_split_by_headings_no_headings(self):
        text = "Just some text.\n\nAnother paragraph."
        sections = _split_by_headings(text)
        # Falls back to paragraph splitting
        assert len(sections) == 2

    def test_split_by_paragraphs(self):
        text = "Para 1.\n\nPara 2.\n\nPara 3."
        paras = _split_by_paragraphs(text)
        assert len(paras) == 3

    def test_split_code(self):
        text = "import os\n\ndef foo():\n    pass\n\ndef bar():\n    pass"
        blocks = _split_code(text)
        assert len(blocks) >= 2

    def test_split_with_overlap(self):
        # Build text with known line lengths
        lines = [f"Line {i} content here" for i in range(20)]
        text = "\n".join(lines)
        chunks = _split_with_overlap(text, chunk_size=100, overlap=20)
        assert len(chunks) >= 2
        # First chunk should be smaller than chunk_size + some margin
        assert len(chunks[0]) <= 200  # generous bound


class TestSupportedFile:
    def test_markdown_supported(self):
        assert _is_supported_file(Path("notes.md"))
        assert _is_supported_file(Path("README.markdown"))

    def test_text_supported(self):
        assert _is_supported_file(Path("readme.txt"))

    def test_code_supported(self):
        assert _is_supported_file(Path("main.py"))
        assert _is_supported_file(Path("app.js"))
        assert _is_supported_file(Path("lib.rs"))

    def test_pdf_supported(self):
        assert _is_supported_file(Path("doc.pdf"))

    def test_binary_not_supported(self):
        assert not _is_supported_file(Path("image.png"))
        assert not _is_supported_file(Path("video.mp4"))
        assert not _is_supported_file(Path("archive.zip"))

    def test_makefile_supported(self):
        assert _is_supported_file(Path("Makefile"))
        assert _is_supported_file(Path("Dockerfile"))


# ---------------------------------------------------------------------------
# KnowledgeBase tests
# ---------------------------------------------------------------------------


class TestKnowledgeBase:
    def _make_kb(self, td: str, **kwargs) -> KnowledgeBase:
        db_path = Path(td) / "test_kb.sqlite"
        return KnowledgeBase(db_path=db_path, **kwargs)

    def test_empty_kb_stats(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            s = kb.stats()
            assert s["documents"] == 0
            assert s["chunks"] == 0
            assert s["total_size"] == 0
            kb.close()

    def test_add_text_file(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            # Create a test file
            test_file = Path(td) / "test.txt"
            test_file.write_text("Hello world. This is a test document about Python programming.")
            result = kb.add(str(test_file))
            assert result["added"] == 1
            assert result["errors"] == 0
            s = kb.stats()
            assert s["documents"] == 1
            assert s["chunks"] >= 1
            kb.close()

    def test_add_markdown_file(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "notes.md"
            test_file.write_text(
                "# My Notes\n\n"
                "## Section 1\n\nThis is about machine learning.\n\n"
                "## Section 2\n\nThis is about databases.\n"
            )
            result = kb.add(str(test_file))
            assert result["added"] == 1
            kb.close()

    def test_add_directory(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            # Create multiple files
            docs_dir = Path(td) / "docs"
            docs_dir.mkdir()
            (docs_dir / "a.md").write_text("# Doc A\n\nContent A.")
            (docs_dir / "b.txt").write_text("Content B.")
            (docs_dir / "c.png").write_bytes(b"\x89PNG")  # unsupported
            result = kb.add(str(docs_dir))
            assert result["added"] == 2  # a.md + b.txt, not c.png
            kb.close()

    def test_add_skips_unchanged_files(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "test.txt"
            test_file.write_text("Content that stays the same.")
            r1 = kb.add(str(test_file))
            assert r1["added"] == 1
            r2 = kb.add(str(test_file))
            assert r2["skipped"] == 1
            assert r2["added"] == 0
            kb.close()

    def test_add_updates_changed_files(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "test.txt"
            test_file.write_text("Original content.")
            kb.add(str(test_file))
            test_file.write_text("Updated content with new information.")
            r2 = kb.add(str(test_file))
            assert r2["updated"] == 1
            kb.close()

    def test_search_basic(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "python.md"
            test_file.write_text(
                "# Python Guide\n\n"
                "Python is a programming language.\n\n"
                "It supports object-oriented programming.\n"
            )
            kb.add(str(test_file))
            results = kb.search("programming language")
            assert len(results) >= 1
            assert any("Python" in r["content"] or "programming" in r["content"] for r in results)
            kb.close()

    def test_search_empty_query(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            results = kb.search("")
            assert results == []
            kb.close()

    def test_search_no_results(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            results = kb.search("nonexistent_xyzzy_term")
            assert results == []
            kb.close()

    def test_search_with_filter(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            dir_a = Path(td) / "dir_a"
            dir_b = Path(td) / "dir_b"
            dir_a.mkdir()
            dir_b.mkdir()
            (dir_a / "a.txt").write_text("Python programming guide.")
            (dir_b / "b.txt").write_text("Python web framework.")
            kb.add(str(dir_a))
            kb.add(str(dir_b))
            # Filter to dir_a only
            results = kb.search("Python", filter_path=str(dir_a))
            assert all(str(dir_a) in r["source"] for r in results)
            kb.close()

    def test_search_source_attribution(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "attributed.md"
            test_file.write_text("# Knowledge\n\nThis chunk has source attribution.")
            kb.add(str(test_file))
            results = kb.search("attribution")
            assert len(results) >= 1
            assert results[0]["source"] == str(test_file.resolve())
            assert results[0]["title"] == "Knowledge"
            assert "score" in results[0]
            kb.close()

    def test_search_top_k(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            for i in range(10):
                f = Path(td) / f"doc{i}.txt"
                f.write_text(f"Document {i} about testing search results.")
            kb.add(td)
            results = kb.search("testing", top_k=3)
            assert len(results) <= 3
            kb.close()

    def test_remove_document(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "removeme.txt"
            test_file.write_text("Content to be removed.")
            kb.add(str(test_file))
            assert kb.stats()["documents"] == 1
            result = kb.remove(str(test_file))
            assert result["removed"] is True
            assert kb.stats()["documents"] == 0
            kb.close()

    def test_remove_nonexistent(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            result = kb.remove("/nonexistent/path.txt")
            assert result["removed"] is False
            kb.close()

    def test_list_documents(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            f1 = Path(td) / "doc1.md"
            f1.write_text("# Doc 1\n\nContent 1.")
            f2 = Path(td) / "doc2.txt"
            f2.write_text("Content 2.")
            kb.add(str(f1))
            kb.add(str(f2))
            docs = kb.list_documents()
            assert len(docs) == 2
            assert all("path" in d for d in docs)
            assert all("filename" in d for d in docs)
            assert all("chunks" in d for d in docs)
            kb.close()

    def test_list_empty(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            assert kb.list_documents() == []
            kb.close()

    def test_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "rebuild_test.txt"
            test_file.write_text("Rebuild test content about algorithms.")
            kb.add(str(test_file))
            result = kb.rebuild()
            assert result["added"] == 1
            # Verify search still works after rebuild
            results = kb.search("algorithms")
            assert len(results) >= 1
            kb.close()

    def test_rebuild_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "will_delete.txt"
            test_file.write_text("Temporary content.")
            kb.add(str(test_file))
            test_file.unlink()
            result = kb.rebuild()
            assert result["errors"] == 1
            kb.close()

    def test_title_extraction_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "titled.md"
            test_file.write_text("# My Great Document\n\nSome content here.")
            kb.add(str(test_file))
            docs = kb.list_documents()
            assert docs[0]["title"] == "My Great Document"
            kb.close()

    def test_title_extraction_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            test_file = Path(td) / "my-file-name.txt"
            test_file.write_text("No heading, just text.")
            kb.add(str(test_file))
            docs = kb.list_documents()
            assert docs[0]["title"] == "My File Name"
            kb.close()

    def test_reindex_auto_paths(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            auto_dir = Path(td) / "auto_notes"
            auto_dir.mkdir()
            (auto_dir / "note1.md").write_text("# Note 1\n\nAuto indexed content.")
            (auto_dir / "note2.txt").write_text("Second auto note.")
            result = kb.reindex_auto_paths([str(auto_dir)])
            assert result["added"] == 2
            # Re-index should skip unchanged
            result2 = kb.reindex_auto_paths([str(auto_dir)])
            assert result2["skipped"] == 2
            kb.close()

    def test_reindex_auto_paths_removes_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            auto_dir = Path(td) / "watched"
            auto_dir.mkdir()
            f = auto_dir / "temp.txt"
            f.write_text("Temporary content.")
            kb.reindex_auto_paths([str(auto_dir)])
            assert kb.stats()["documents"] == 1
            f.unlink()
            result = kb.reindex_auto_paths([str(auto_dir)])
            assert result["removed"] == 1
            assert kb.stats()["documents"] == 0
            kb.close()

    def test_add_nonexistent_path(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td)
            result = kb.add("/nonexistent/path/foo.txt")
            assert result["errors"] == 1
            assert result["added"] == 0
            kb.close()

    def test_chunk_size_config(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb(td, chunk_size=100, chunk_overlap=10)
            test_file = Path(td) / "large.txt"
            # Create content with line breaks so _split_with_overlap can split
            test_file.write_text("\n".join(f"Line {i} with some content." for i in range(30)))
            kb.add(str(test_file))
            s = kb.stats()
            # With small chunk_size, should have multiple chunks
            assert s["chunks"] >= 2
            kb.close()


# ---------------------------------------------------------------------------
# KnowledgeBaseConfig model tests
# ---------------------------------------------------------------------------


class TestKnowledgeBaseConfig:
    def test_default_config(self):
        from creel.models import KnowledgeBaseConfig

        cfg = KnowledgeBaseConfig()
        assert cfg.enabled is False
        assert cfg.store == "sqlite"
        assert cfg.chunk_size == 512
        assert cfg.chunk_overlap == 50
        assert cfg.auto_index == []

    def test_invalid_store(self):
        from creel.models import KnowledgeBaseConfig

        with pytest.raises(Exception):
            KnowledgeBaseConfig(store="postgres")

    def test_auto_index_expansion(self):
        from creel.models import KnowledgeBaseConfig

        cfg = KnowledgeBaseConfig(auto_index=["~/docs"])
        assert cfg.auto_index[0] != "~/docs"  # should be expanded
        assert "~" not in cfg.auto_index[0]


# ---------------------------------------------------------------------------
# Tool integration tests
# ---------------------------------------------------------------------------


class TestKBToolHandling:
    def test_handle_kb_search(self):
        from creel.tools import _handle_kb_tool

        with tempfile.TemporaryDirectory() as td:
            from creel.knowledge_base import KnowledgeBase

            kb = KnowledgeBase(db_path=Path(td) / "test.sqlite")
            f = Path(td) / "doc.txt"
            f.write_text("Python is a great programming language for data science.")
            kb.add(str(f))

            result = _handle_kb_tool("kb_search", {"query": "Python"}, kb)
            data = json.loads(result)
            assert "results" in data
            assert len(data["results"]) >= 1
            kb.close()

    def test_handle_kb_search_no_results(self):
        from creel.tools import _handle_kb_tool

        with tempfile.TemporaryDirectory() as td:
            from creel.knowledge_base import KnowledgeBase

            kb = KnowledgeBase(db_path=Path(td) / "test.sqlite")
            result = _handle_kb_tool("kb_search", {"query": "nonexistent"}, kb)
            data = json.loads(result)
            assert data["results"] == []
            kb.close()

    @patch("creel.tools._is_kb_path_safe", return_value=True)
    def test_handle_kb_add(self, _mock_safe):
        from creel.tools import _handle_kb_tool

        with tempfile.TemporaryDirectory() as td:
            from creel.knowledge_base import KnowledgeBase

            kb = KnowledgeBase(db_path=Path(td) / "test.sqlite")
            f = Path(td) / "add_test.txt"
            f.write_text("Content to add.")
            result = _handle_kb_tool("kb_add", {"path": str(f)}, kb)
            data = json.loads(result)
            assert data["added"] == 1
            kb.close()

    def test_handle_kb_list(self):
        from creel.tools import _handle_kb_tool

        with tempfile.TemporaryDirectory() as td:
            from creel.knowledge_base import KnowledgeBase

            kb = KnowledgeBase(db_path=Path(td) / "test.sqlite")
            result = _handle_kb_tool("kb_list", {}, kb)
            data = json.loads(result)
            assert data["documents"] == []
            kb.close()

    def test_handle_kb_stats(self):
        from creel.tools import _handle_kb_tool

        with tempfile.TemporaryDirectory() as td:
            from creel.knowledge_base import KnowledgeBase

            kb = KnowledgeBase(db_path=Path(td) / "test.sqlite")
            result = _handle_kb_tool("kb_stats", {}, kb)
            data = json.loads(result)
            assert data["documents"] == 0
            assert data["chunks"] == 0
            kb.close()

    def test_handle_unknown_kb_tool(self):
        from creel.tools import _handle_kb_tool

        with tempfile.TemporaryDirectory() as td:
            from creel.knowledge_base import KnowledgeBase

            kb = KnowledgeBase(db_path=Path(td) / "test.sqlite")
            result = _handle_kb_tool("kb_unknown", {}, kb)
            data = json.loads(result)
            assert "error" in data
            kb.close()


# ---------------------------------------------------------------------------
# Tool definition tests
# ---------------------------------------------------------------------------


class TestBuildToolDefinitions:
    def test_kb_tools_included_when_flag_set(self):
        from creel.skills.registry import SkillRegistry
        from creel.tools import build_tool_definitions

        registry = SkillRegistry()
        defs = build_tool_definitions(registry, {}, include_kb_tools=True)
        names = {d["name"] for d in defs}
        assert "kb_search" in names
        assert "kb_add" in names
        assert "kb_list" in names
        assert "kb_stats" in names

    def test_kb_tools_excluded_by_default(self):
        from creel.skills.registry import SkillRegistry
        from creel.tools import build_tool_definitions

        registry = SkillRegistry()
        defs = build_tool_definitions(registry, {})
        names = {d["name"] for d in defs}
        assert "kb_search" not in names


# ---------------------------------------------------------------------------
# Embedding helper tests
# ---------------------------------------------------------------------------


class TestEmbeddingHelpers:
    def test_encode_decode_roundtrip(self):
        original = [0.1, 0.2, 0.3, 0.4, 0.5]
        blob = _encode_embedding(original)
        decoded = _decode_embedding(blob)
        assert len(decoded) == len(original)
        for a, b in zip(original, decoded, strict=True):
            assert abs(a - b) < 1e-6

    def test_encode_empty(self):
        blob = _encode_embedding([])
        assert blob == b""
        decoded = _decode_embedding(blob)
        assert decoded == []

    def test_cosine_similarity_identical(self):
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_cosine_similarity_opposite(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6

    def test_cosine_similarity_zero_vector(self):
        a = [0.0, 0.0]
        b = [1.0, 2.0]
        assert _cosine_similarity(a, b) == 0.0


# ---------------------------------------------------------------------------
# Mock embedder for vector search tests
# ---------------------------------------------------------------------------


class _MockEmbedder:
    """Deterministic mock for sentence_transformers.SentenceTransformer.

    Produces 8-dimensional embeddings derived from content hashes.
    """

    def encode(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            h = hashlib.md5(text.encode(), usedforsecurity=False).digest()
            emb = [b / 255.0 for b in h[:8]]
            result.append(emb)
        return result


# ---------------------------------------------------------------------------
# Vector search tests (using mock embedder)
# ---------------------------------------------------------------------------


class TestVectorSearch:
    def _make_kb_with_embedder(self, td: str) -> KnowledgeBase:
        db_path = Path(td) / "vec_kb.sqlite"
        kb = KnowledgeBase(db_path=db_path)
        # Inject mock embedder to avoid sentence-transformers dependency
        kb._embedder = _MockEmbedder()
        kb._embedder_loaded = True
        return kb

    def test_vector_search_returns_results(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb_with_embedder(td)
            f = Path(td) / "doc.txt"
            f.write_text("Python is a great programming language for data science.")
            kb.add(str(f))
            results = kb.search("Python programming")
            assert len(results) >= 1
            assert results[0]["source"] == str(f.resolve())
            assert "score" in results[0]
            kb.close()

    def test_vector_search_source_attribution(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb_with_embedder(td)
            f = Path(td) / "attributed.md"
            f.write_text("# Guide\n\nVector search with attribution test.")
            kb.add(str(f))
            results = kb.search("attribution")
            assert len(results) >= 1
            assert results[0]["source"] == str(f.resolve())
            assert results[0]["title"] == "Guide"
            kb.close()

    def test_vector_search_with_filter(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb_with_embedder(td)
            dir_a = Path(td) / "dir_a"
            dir_b = Path(td) / "dir_b"
            dir_a.mkdir()
            dir_b.mkdir()
            (dir_a / "a.txt").write_text("Content from directory A about Python.")
            (dir_b / "b.txt").write_text("Content from directory B about Python.")
            kb.add(str(dir_a))
            kb.add(str(dir_b))
            results = kb.search("Python", filter_path=str(dir_a))
            assert all(str(dir_a) in r["source"] for r in results)
            kb.close()

    def test_vector_search_top_k(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb_with_embedder(td)
            for i in range(10):
                f = Path(td) / f"doc{i}.txt"
                f.write_text(f"Document {i} about testing vector search results.")
            kb.add(td)
            results = kb.search("testing", top_k=3)
            assert len(results) <= 3
            kb.close()

    def test_embeddings_stored_in_db(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb_with_embedder(td)
            f = Path(td) / "emb_test.txt"
            f.write_text("Content to check embedding storage.")
            kb.add(str(f))
            assert kb._conn is not None
            count = kb._conn.execute("SELECT COUNT(*) FROM kb_embeddings").fetchone()[0]
            assert count >= 1
            kb.close()

    def test_embeddings_removed_with_document(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb_with_embedder(td)
            f = Path(td) / "removeme.txt"
            f.write_text("Content with embeddings to remove.")
            kb.add(str(f))
            assert kb._conn is not None
            count_before = kb._conn.execute("SELECT COUNT(*) FROM kb_embeddings").fetchone()[0]
            assert count_before >= 1
            kb.remove(str(f))
            count_after = kb._conn.execute("SELECT COUNT(*) FROM kb_embeddings").fetchone()[0]
            assert count_after == 0
            kb.close()

    def test_rebuild_regenerates_embeddings(self):
        with tempfile.TemporaryDirectory() as td:
            kb = self._make_kb_with_embedder(td)
            f = Path(td) / "rebuild_emb.txt"
            f.write_text("Rebuild embedding test content.")
            kb.add(str(f))
            result = kb.rebuild()
            assert result["added"] == 1
            assert kb._conn is not None
            count = kb._conn.execute("SELECT COUNT(*) FROM kb_embeddings").fetchone()[0]
            assert count >= 1
            kb.close()

    def test_fts_fallback_when_no_embedder(self):
        """Without embedder, search should fall back to FTS5."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "fts_only.sqlite"
            kb = KnowledgeBase(db_path=db_path)
            # Mark embedder as loaded but None (no sentence-transformers)
            kb._embedder = None
            kb._embedder_loaded = True
            f = Path(td) / "fts_doc.txt"
            f.write_text("FTS5 fallback search for knowledge base testing.")
            kb.add(str(f))
            results = kb.search("knowledge base")
            assert len(results) >= 1
            kb.close()


class TestKnowledgeBaseConfigEmbedding:
    def test_embedding_model_default(self):
        from creel.models import KnowledgeBaseConfig

        cfg = KnowledgeBaseConfig()
        assert cfg.embedding_model == "all-MiniLM-L6-v2"

    def test_embedding_model_custom(self):
        from creel.models import KnowledgeBaseConfig

        cfg = KnowledgeBaseConfig(embedding_model="paraphrase-MiniLM-L3-v2")
        assert cfg.embedding_model == "paraphrase-MiniLM-L3-v2"
