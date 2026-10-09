"""Local RAG index over OEM source code (ChromaDB + sentence-transformers).

Chunk ids are derived from file path + line, so re-indexing updates chunks
in place instead of duplicating them. Chunks are small enough for the
embedding model to see all of their text. The embedding model can be a
local path, so offline labs never reach out to the model hub.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, List

logger = logging.getLogger(__name__)

SOURCE_EXTENSIONS = {".cpp", ".cc", ".h", ".hpp", ".c", ".java", ".kt", ".aidl", ".rs"}
SKIP_DIRS = {".git", ".repo", "out", "prebuilts", "node_modules", "__pycache__"}
CHUNK_LINES = 60
CHUNK_OVERLAP = 10
MAX_FILE_BYTES = 2 * 1024 * 1024
BATCH = 2000


def _ensure_modern_sqlite() -> None:
    """ChromaDB needs SQLite >= 3.35; Ubuntu 20.04 ships 3.31.

    Swap in pysqlite3-binary (part of the [ai] extra) for chromadb's import.
    Modules that already imported the stdlib sqlite3 keep using it.
    """
    if tuple(int(p) for p in sqlite3.sqlite_version.split(".")[:2]) >= (3, 35):
        return
    try:
        import pysqlite3

        sys.modules["sqlite3"] = pysqlite3
    except ImportError:
        logger.error(
            "System SQLite %s is too old for ChromaDB; pip install pysqlite3-binary "
            "(included in the [ai] extra)", sqlite3.sqlite_version
        )


class OEMCodeIndexer:
    def __init__(self, db_path: str, source_paths: List[str], embedding_model: str = "all-MiniLM-L6-v2"):
        self.db_path = db_path
        self.source_paths = source_paths
        self.embedding_model = embedding_model
        self.collection: Any = None  # chromadb collection, created on first use

    def _lazy_init(self):
        if self.collection is not None:
            return
        _ensure_modern_sqlite()
        import chromadb
        from chromadb.config import Settings
        from chromadb.utils import embedding_functions

        os.makedirs(self.db_path, exist_ok=True)
        # No usage telemetry from an on-prem OEM host
        client = chromadb.PersistentClient(path=self.db_path, settings=Settings(anonymized_telemetry=False))
        emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=self.embedding_model)
        self.collection = client.get_or_create_collection(name="oem_source_code", embedding_function=emb_fn)

    @staticmethod
    def chunk_file(path: Path) -> List[tuple]:
        """[(chunk_id, text, start_line)] for one source file."""
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return []
            lines = path.read_text(encoding="utf-8", errors="ignore").split("\n")
        except OSError:
            return []
        chunks = []
        step = CHUNK_LINES - CHUNK_OVERLAP
        for start in range(0, max(len(lines), 1), step):
            text = "\n".join(lines[start:start + CHUNK_LINES])
            if len(text.strip()) > 50:
                chunk_id = hashlib.sha1(f"{path}:{start}".encode()).hexdigest()
                chunks.append((chunk_id, text, start + 1))
            if start + CHUNK_LINES >= len(lines):
                break
        return chunks

    def iter_source_files(self):
        for base in self.source_paths:
            if not Path(base).exists():
                logger.warning("Source path %s does not exist. Skipping.", base)
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in files:
                    if os.path.splitext(name)[1].lower() in SOURCE_EXTENSIONS:
                        yield Path(root) / name

    def index_codebase(self) -> int:
        """Index (or re-index) source_paths; returns the number of chunks upserted."""
        self._lazy_init()
        ids, docs, metas, total = [], [], [], 0
        for path in self.iter_source_files():
            for chunk_id, text, line in self.chunk_file(path):
                ids.append(chunk_id)
                docs.append(text)
                metas.append({"file": str(path), "start_line": line})
                if len(ids) >= BATCH:
                    self.collection.upsert(ids=ids, documents=docs, metadatas=metas)
                    total += len(ids)
                    ids, docs, metas = [], [], []
                    logger.info("Indexed %s chunks...", total)
        if ids:
            self.collection.upsert(ids=ids, documents=docs, metadatas=metas)
            total += len(ids)
        logger.info("Indexing complete: %s chunks", total)
        return total

    def search(self, query: str, top_k: int = 3) -> str:
        """Relevant OEM code for a failure, or "" when nothing is indexed."""
        try:
            self._lazy_init()
            if self.collection.count() == 0:
                return ""
            results = self.collection.query(query_texts=[query], n_results=top_k)
        except Exception as e:
            logger.warning("Code search unavailable: %s", e)
            return ""
        context = []
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            context.append(f"--- File: {meta['file']} (line {meta['start_line']}) ---\n{doc}\n")
        return "\n".join(context)
