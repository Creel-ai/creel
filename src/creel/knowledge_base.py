"""Knowledge base - RAG document store with semantic chunking and FTS5 search.

Indexes personal documents (markdown, text, PDF, code) into a local SQLite
FTS5 store for retrieval-augmented generation. Documents are chunked by
paragraphs/sections and indexed for full-text search with BM25 ranking.

Configuration in agent.yaml:
    knowledge_base:
      store: sqlite
      chunk_size: 512
      chunk_overlap: 50
      auto_index:
        - ~/Documents/notes/
        - ~/projects/docs/
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum file size to index (50 MB) — prevents OOM on large files
_MAX_FILE_SIZE = 50 * 1024 * 1024

# Maximum files per add() call to prevent runaway indexing
_MAX_FILES_PER_ADD = 10_000

# Maximum query length in characters
_MAX_QUERY_LENGTH = 10_000


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards (%, _) in a value."""
    return value.replace("%", r"\%").replace("_", r"\_")


# File extensions we know how to ingest
_TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".rst",
        ".org",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".log",
    }
)

_CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".rb",
        ".php",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".html",
        ".css",
        ".scss",
        ".swift",
        ".kt",
        ".lua",
        ".r",
        ".R",
        ".m",
        ".el",
        ".vim",
        ".ps1",
        ".bat",
        ".dockerfile",
        ".makefile",
        ".cmake",
    }
)

_SUPPORTED_EXTENSIONS = _TEXT_EXTENSIONS | _CODE_EXTENSIONS | {".pdf"}

# Heading patterns for semantic chunking
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"^```", re.MULTILINE)


def _file_content_hash(content: bytes) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content).hexdigest()


def _encode_embedding(embedding: list[float]) -> bytes:
    """Encode a float list to bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _decode_embedding(blob: bytes) -> list[float]:
    """Decode a BLOB back to a float list."""
    n = len(blob) // 4  # float32 = 4 bytes
    return list(struct.unpack(f"{n}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _is_supported_file(path: Path) -> bool:
    """Check if a file has a supported extension for ingestion."""
    suffix = path.suffix.lower()
    if suffix in _SUPPORTED_EXTENSIONS:
        return True
    # Also check by name for files like Makefile, Dockerfile
    name_lower = path.name.lower()
    return name_lower in ("makefile", "dockerfile", "rakefile", "gemfile")


def _read_file_text(path: Path) -> str | None:
    """Read text content from a supported file. Returns None on failure."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _read_pdf(path)

    # Try reading as text
    try:
        return path.read_text(errors="replace")
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None


def _read_pdf(path: Path) -> str | None:
    """Extract text from a PDF file. Returns None if no PDF library available."""
    # Try PyMuPDF (fitz) first, then pdfplumber, then fall back
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(path))
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        return "\n\n".join(pages)
    except ImportError:
        pass

    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n\n".join(pages)
    except ImportError:
        pass

    logger.warning(
        "No PDF library available (install PyMuPDF or pdfplumber). Skipping %s",
        path,
    )
    return None


def chunk_text(
    text: str,
    source_path: str,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
) -> list[dict]:
    """Split text into semantic chunks.

    Uses paragraph/section boundaries for natural splits rather than
    fixed-size windows. Falls back to paragraph splitting for plain text.

    Args:
        text: The full document text.
        source_path: Path of the source file (for metadata).
        chunk_size: Target chunk size in characters.
        chunk_overlap: Overlap between consecutive chunks in characters.

    Returns:
        List of chunk dicts with 'content', 'source', 'chunk_index' keys.
    """
    if not text.strip():
        return []

    suffix = Path(source_path).suffix.lower()

    # Choose splitting strategy based on file type
    if suffix in (".md", ".markdown", ".rst", ".org"):
        sections = _split_by_headings(text)
    elif suffix in _CODE_EXTENSIONS or Path(source_path).name.lower() in (
        "makefile",
        "dockerfile",
    ):
        sections = _split_code(text)
    else:
        sections = _split_by_paragraphs(text)

    # Further split sections that exceed chunk_size
    chunks = []
    for section in sections:
        if len(section) <= chunk_size:
            if section.strip():
                chunks.append(section)
        else:
            # Split large sections with overlap
            chunks.extend(_split_with_overlap(section, chunk_size, chunk_overlap))

    return [
        {
            "content": chunk.strip(),
            "source": source_path,
            "chunk_index": i,
        }
        for i, chunk in enumerate(chunks)
        if chunk.strip()
    ]


def _split_by_headings(text: str) -> list[str]:
    """Split markdown/rst text by headings."""
    # Split at heading boundaries, keeping the heading with its section
    parts = _HEADING_RE.split(text)
    headings = _HEADING_RE.findall(text)

    if not headings:
        return _split_by_paragraphs(text)

    sections = []
    # First part (before any heading)
    if parts[0].strip():
        sections.append(parts[0])

    # Rejoin headings with their content
    for i, heading in enumerate(headings):
        section_text = heading + parts[i + 1] if i + 1 < len(parts) else heading
        if section_text.strip():
            sections.append(section_text)

    return sections if sections else _split_by_paragraphs(text)


def _split_by_paragraphs(text: str) -> list[str]:
    """Split text by double newlines (paragraphs)."""
    paragraphs = re.split(r"\n\s*\n", text)
    return [p for p in paragraphs if p.strip()]


def _split_code(text: str) -> list[str]:
    """Split code files by function/class definitions or blank-line groups."""
    # Split on blank lines that separate logical blocks
    blocks = re.split(r"\n\n+", text)
    return [b for b in blocks if b.strip()]


def _split_with_overlap(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks by character count, respecting line boundaries."""
    lines = text.split("\n")
    chunks = []
    current_chunk: list[str] = []
    current_size = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current_size + line_len > chunk_size and current_chunk:
            chunks.append("\n".join(current_chunk))
            # Keep overlap by retaining last few lines
            overlap_lines: list[str] = []
            overlap_size = 0
            for prev_line in reversed(current_chunk):
                if overlap_size + len(prev_line) + 1 > overlap:
                    break
                overlap_lines.insert(0, prev_line)
                overlap_size += len(prev_line) + 1
            current_chunk = overlap_lines
            current_size = overlap_size

        current_chunk.append(line)
        current_size += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


class KnowledgeBase:
    """SQLite FTS5-backed knowledge base for document search.

    Documents are chunked, indexed, and searchable via full-text search
    with BM25 ranking. The SQLite database is the primary store.
    """

    def __init__(
        self,
        db_path: str | Path,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        embedding_model: str = "all-MiniLM-L6-v2",
    ):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._embedding_model_name = embedding_model
        self._embedder: Any = None
        self._embedder_loaded = False
        self._conn: sqlite3.Connection | None = None

        try:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema()
        except sqlite3.Error as exc:
            logger.error("Failed to initialize knowledge base: %s", exc)
            raise

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kb_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                file_size INTEGER NOT NULL DEFAULT 0,
                indexed_at REAL NOT NULL,
                title TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS kb_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                FOREIGN KEY (doc_id) REFERENCES kb_documents(id) ON DELETE CASCADE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
                content,
                source,
                title,
                tokenize='porter unicode61'
            );

            CREATE TABLE IF NOT EXISTS kb_fts_map (
                fts_rowid INTEGER PRIMARY KEY,
                chunk_id INTEGER NOT NULL,
                doc_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_embeddings (
                chunk_id INTEGER PRIMARY KEY,
                embedding BLOB NOT NULL,
                FOREIGN KEY (chunk_id) REFERENCES kb_chunks(id) ON DELETE CASCADE
            );
            """
        )
        self._conn.commit()

    def _get_embedder(self) -> Any:
        """Lazily load the sentence-transformers embedding model."""
        if not self._embedder_loaded:
            self._embedder_loaded = True
            try:
                from sentence_transformers import SentenceTransformer

                self._embedder = SentenceTransformer(self._embedding_model_name)
                logger.info("Loaded embedding model: %s", self._embedding_model_name)
            except ImportError:
                logger.info("sentence-transformers not installed; using FTS5-only search")
            except Exception as exc:
                logger.warning("Failed to load embedding model: %s", exc)
        return self._embedder

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        embedder = self._get_embedder()
        if embedder is None:
            return []
        result = embedder.encode(texts)
        if hasattr(result, "tolist"):
            return result.tolist()
        return list(result)

    def add(self, path: str | Path, recursive: bool = True) -> dict[str, Any]:
        """Index a file or directory.

        Args:
            path: Path to a file or directory.
            recursive: If True, recursively index directories.

        Returns:
            Dict with 'added', 'updated', 'skipped', 'errors' counts.
        """
        path = Path(os.path.expanduser(str(path))).resolve()
        stats: dict[str, Any] = {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "files": [],
        }

        if path.is_file():
            self._index_file(path, stats)
        elif path.is_dir():
            self._index_directory(path, recursive, stats)
        else:
            stats["errors"] += 1
            logger.error("Path does not exist: %s", path)

        return stats

    def _index_directory(self, dir_path: Path, recursive: bool, stats: dict[str, Any]) -> None:
        """Index all supported files in a directory."""
        resolved_root = dir_path.resolve()
        pattern = "**/*" if recursive else "*"
        file_count = 0
        for file_path in sorted(dir_path.glob(pattern)):
            if not file_path.is_file() or not _is_supported_file(file_path):
                continue
            # Resolve symlinks and ensure the file is still under the root
            # to prevent symlink-based directory escape.
            try:
                resolved = file_path.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_relative_to(resolved_root):
                logger.debug("Skipping symlink outside root: %s -> %s", file_path, resolved)
                continue
            file_count += 1
            if file_count > _MAX_FILES_PER_ADD:
                logger.warning(
                    "Stopping indexing: exceeded %d file limit in %s",
                    _MAX_FILES_PER_ADD,
                    dir_path,
                )
                break
            self._index_file(resolved, stats)

    def _index_file(self, file_path: Path, stats: dict[str, Any]) -> None:
        """Index a single file, skipping if unchanged."""
        assert self._conn is not None

        # Guard against very large files
        try:
            file_size = file_path.stat().st_size
        except OSError as exc:
            logger.warning("Cannot stat %s: %s", file_path, exc)
            stats["errors"] += 1
            return
        if file_size > _MAX_FILE_SIZE:
            logger.warning("Skipping %s: file too large (%d bytes)", file_path, file_size)
            stats["skipped"] += 1
            return

        try:
            content_bytes = file_path.read_bytes()
        except OSError as exc:
            logger.warning("Cannot read %s: %s", file_path, exc)
            stats["errors"] += 1
            return

        content_hash = _file_content_hash(content_bytes)
        path_str = str(file_path)

        # Check if already indexed with same hash
        row = self._conn.execute(
            "SELECT id, content_hash FROM kb_documents WHERE path = ?",
            (path_str,),
        ).fetchone()

        if row and row[1] == content_hash:
            stats["skipped"] += 1
            return

        # Read text content
        text = _read_file_text(file_path)
        if text is None:
            stats["errors"] += 1
            return

        # Extract title from content
        title = self._extract_title(text, file_path)

        # Chunk the document
        chunks = chunk_text(
            text,
            path_str,
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
        )

        if not chunks:
            stats["skipped"] += 1
            return

        # Remove old data if updating
        if row:
            self._remove_doc_data(row[0])
            stats["updated"] += 1
        else:
            stats["added"] += 1

        # Insert document record
        cursor = self._conn.execute(
            "INSERT OR REPLACE INTO kb_documents "
            "(path, filename, content_hash, chunk_count, file_size, indexed_at, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                path_str,
                file_path.name,
                content_hash,
                len(chunks),
                len(content_bytes),
                time.time(),
                title,
            ),
        )
        doc_id = cursor.lastrowid

        # Insert chunks and FTS entries
        chunk_ids = []
        for chunk in chunks:
            chunk_cursor = self._conn.execute(
                "INSERT INTO kb_chunks (doc_id, chunk_index, content) VALUES (?, ?, ?)",
                (doc_id, chunk["chunk_index"], chunk["content"]),
            )
            chunk_id = chunk_cursor.lastrowid
            chunk_ids.append(chunk_id)

            # Insert into FTS
            self._conn.execute(
                "INSERT INTO kb_fts (content, source, title) VALUES (?, ?, ?)",
                (chunk["content"], path_str, title),
            )
            fts_rowid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            self._conn.execute(
                "INSERT INTO kb_fts_map (fts_rowid, chunk_id, doc_id) VALUES (?, ?, ?)",
                (fts_rowid, chunk_id, doc_id),
            )

        # Compute and store embeddings if model is available
        embeddings = self._embed_texts([c["content"] for c in chunks])
        if embeddings:
            for cid, emb in zip(chunk_ids, embeddings, strict=True):
                self._conn.execute(
                    "INSERT INTO kb_embeddings (chunk_id, embedding) VALUES (?, ?)",
                    (cid, _encode_embedding(emb)),
                )

        self._conn.commit()
        stats["files"].append(file_path.name)
        logger.info("Indexed %s (%d chunks)", file_path.name, len(chunks))

    def _extract_title(self, text: str, path: Path) -> str:
        """Extract a title from the document content or filename."""
        # Try markdown heading
        for line in text.split("\n")[:10]:
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("##"):
                return stripped[2:].strip()

        # Fall back to filename without extension
        return path.stem.replace("_", " ").replace("-", " ").title()

    def _remove_doc_data(self, doc_id: int) -> None:
        """Remove all data for a document."""
        assert self._conn is not None

        # Remove embeddings via chunk_id
        self._conn.execute(
            "DELETE FROM kb_embeddings WHERE chunk_id IN "
            "(SELECT id FROM kb_chunks WHERE doc_id = ?)",
            (doc_id,),
        )
        # Remove FTS entries via mapping
        self._conn.execute(
            "DELETE FROM kb_fts WHERE rowid IN (SELECT fts_rowid FROM kb_fts_map WHERE doc_id = ?)",
            (doc_id,),
        )
        self._conn.execute("DELETE FROM kb_fts_map WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_path: str | None = None,
    ) -> list[dict]:
        """Search the knowledge base.

        Uses vector cosine similarity when embeddings are available,
        falling back to FTS5 BM25 ranking otherwise.

        Args:
            query: Search query string.
            top_k: Maximum number of results to return.
            filter_path: Optional path prefix to filter results.

        Returns:
            List of result dicts with 'content', 'source', 'title', 'score' keys.
        """
        if not self._conn:
            return []

        if not query.strip():
            return []

        # Truncate excessively long queries to prevent memory issues
        if len(query) > _MAX_QUERY_LENGTH:
            query = query[:_MAX_QUERY_LENGTH]

        # Try vector search if embedder is available
        embedder = self._get_embedder()
        if embedder is not None:
            results = self._vector_search(query, top_k, filter_path)
            if results:
                return results

        # Fall back to FTS5 search
        return self._fts_search(query, top_k, filter_path)

    def _vector_search(
        self,
        query: str,
        top_k: int = 5,
        filter_path: str | None = None,
    ) -> list[dict]:
        """Search using cosine similarity on stored embeddings.

        Note: performs a linear scan over all stored embeddings. This is
        fine for personal document collections (up to ~10k chunks) but
        will degrade for very large corpora.
        """
        assert self._conn is not None

        query_embedding = self._embed_texts([query])
        if not query_embedding:
            return []
        query_vec = query_embedding[0]

        sql = (
            "SELECT c.content, d.path, d.title, e.embedding "
            "FROM kb_embeddings e "
            "JOIN kb_chunks c ON c.id = e.chunk_id "
            "JOIN kb_documents d ON c.doc_id = d.id"
        )
        params: list[str] = []
        if filter_path:
            sql += r" WHERE d.path LIKE ? ESCAPE '\'"
            params.append(f"{_escape_like(filter_path)}%")

        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return []

        scored = []
        for content, source, title, emb_blob in rows:
            emb = _decode_embedding(emb_blob)
            score = _cosine_similarity(query_vec, emb)
            scored.append(
                {
                    "content": content,
                    "source": source,
                    "title": title,
                    "score": round(score, 4),
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def _fts_search(
        self,
        query: str,
        top_k: int = 5,
        filter_path: str | None = None,
    ) -> list[dict]:
        """Search using FTS5 BM25 ranking (fallback when embeddings unavailable)."""
        assert self._conn is not None

        # Always quote the query to prevent FTS5 operator injection
        # (NOT, OR, NEAR, *, ^ etc.). Double-quote escaping inside the phrase.
        escaped = query.replace('"', '""')
        safe_query = f'"{escaped}"'

        try:
            sql = (
                "SELECT f.content, f.source, f.title, bm25(kb_fts) as score "
                "FROM kb_fts f "
                "WHERE kb_fts MATCH ? "
            )
            params: list[str | int] = [safe_query]

            if filter_path:
                sql += r"AND f.source LIKE ? ESCAPE '\' "
                params.append(f"{_escape_like(filter_path)}%")

            sql += "ORDER BY bm25(kb_fts) LIMIT ?"
            params.append(top_k)

            rows = self._conn.execute(sql, params).fetchall()

            return [
                {
                    "content": content,
                    "source": source,
                    "title": title,
                    "score": round(-score, 4),  # bm25() returns negative
                }
                for content, source, title, score in rows
            ]
        except sqlite3.OperationalError:
            logger.debug("FTS5 MATCH failed for query %r", safe_query)
            return []

    def remove(self, path: str | Path) -> dict:
        """Remove a document from the knowledge base.

        Args:
            path: Path of the document to remove.

        Returns:
            Dict with 'removed' boolean and 'path'.
        """
        if not self._conn:
            return {"removed": False, "path": str(path)}

        path = Path(os.path.expanduser(str(path))).resolve()
        path_str = str(path)

        row = self._conn.execute(
            "SELECT id FROM kb_documents WHERE path = ?",
            (path_str,),
        ).fetchone()

        if not row:
            return {"removed": False, "path": path_str, "error": "Document not found"}

        self._remove_doc_data(row[0])
        self._conn.commit()
        logger.info("Removed %s from knowledge base", path_str)
        return {"removed": True, "path": path_str}

    def list_documents(self) -> list[dict]:
        """List all indexed documents.

        Returns:
            List of document info dicts.
        """
        if not self._conn:
            return []

        rows = self._conn.execute(
            "SELECT path, filename, chunk_count, file_size, indexed_at, title "
            "FROM kb_documents ORDER BY indexed_at DESC"
        ).fetchall()

        return [
            {
                "path": row[0],
                "filename": row[1],
                "chunks": row[2],
                "size": row[3],
                "indexed_at": row[4],
                "title": row[5],
            }
            for row in rows
        ]

    def stats(self) -> dict:
        """Get knowledge base statistics.

        Returns:
            Dict with document count, chunk count, total size, etc.
        """
        if not self._conn:
            return {"documents": 0, "chunks": 0, "total_size": 0, "db_size": 0}

        doc_count = self._conn.execute("SELECT COUNT(*) FROM kb_documents").fetchone()[0]
        chunk_count = self._conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        total_size = self._conn.execute(
            "SELECT COALESCE(SUM(file_size), 0) FROM kb_documents"
        ).fetchone()[0]

        db_size = 0
        if self._db_path.exists():
            db_size = self._db_path.stat().st_size

        return {
            "documents": doc_count,
            "chunks": chunk_count,
            "total_size": total_size,
            "db_size": db_size,
        }

    def rebuild(self) -> dict:
        """Rebuild the entire index from stored document paths.

        Re-reads and re-indexes all documents currently in the database.

        Returns:
            Stats dict from the rebuild operation.
        """
        if not self._conn:
            return {"error": "Database not available"}

        # Get all document paths
        rows = self._conn.execute("SELECT path FROM kb_documents").fetchall()
        paths = [row[0] for row in rows]

        # Clear everything
        self._conn.executescript(
            """
            DELETE FROM kb_embeddings;
            DELETE FROM kb_fts_map;
            DELETE FROM kb_fts;
            DELETE FROM kb_chunks;
            DELETE FROM kb_documents;
            """
        )
        self._conn.commit()

        # Re-index
        stats: dict[str, Any] = {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "files": [],
        }
        for path_str in paths:
            path = Path(path_str)
            if path.exists():
                self._index_file(path, stats)
            else:
                stats["errors"] += 1
                logger.warning("File no longer exists: %s", path_str)

        return stats

    def reindex_auto_paths(self, auto_index_paths: list[str]) -> dict:
        """Re-index configured auto-index directories.

        Scans each directory for new/modified files and indexes them.
        Removes documents whose source files no longer exist.

        Args:
            auto_index_paths: List of directory paths to watch.

        Returns:
            Stats dict from the indexing operation.
        """
        stats: dict[str, Any] = {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "removed": 0,
            "files": [],
        }

        indexed_paths: set[str] = set()

        for dir_path_str in auto_index_paths:
            dir_path = Path(os.path.expanduser(dir_path_str)).resolve()
            if not dir_path.is_dir():
                logger.debug("Auto-index path not found: %s", dir_path)
                continue

            for file_path in sorted(dir_path.rglob("*")):
                if file_path.is_file() and _is_supported_file(file_path):
                    indexed_paths.add(str(file_path))
                    self._index_file(file_path, stats)

        # Remove documents from auto-indexed dirs that no longer exist
        if self._conn and auto_index_paths:
            for dir_path_str in auto_index_paths:
                dir_path = Path(os.path.expanduser(dir_path_str)).resolve()
                prefix = str(dir_path)
                rows = self._conn.execute(
                    "SELECT id, path FROM kb_documents WHERE path LIKE ?",
                    (f"{prefix}%",),
                ).fetchall()
                for doc_id, doc_path in rows:
                    if doc_path not in indexed_paths and not Path(doc_path).exists():
                        self._remove_doc_data(doc_id)
                        stats["removed"] += 1
                        logger.info("Removed stale document: %s", doc_path)

            self._conn.commit()

        return stats

    def __enter__(self) -> KnowledgeBase:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
