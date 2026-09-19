"""Local SQLite cache for Gemini embedding vectors."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.settings import Settings, get_settings


@dataclass
class EmbeddingCacheStats:
    """Hit/miss counters for one process run."""

    hits: int = 0
    misses: int = 0

    def record_hit(self) -> None:
        self.hits += 1

    def record_miss(self) -> None:
        self.misses += 1

    def format_summary(self) -> str:
        return f"{self.hits} cache hits, {self.misses} new embeddings"


_stats = EmbeddingCacheStats()
_stats_lock = threading.Lock()


def get_embedding_cache_stats() -> EmbeddingCacheStats:
    return _stats


def reset_embedding_cache_stats() -> None:
    global _stats
    with _stats_lock:
        _stats = EmbeddingCacheStats()


def format_embedding_cache_stats() -> str:
    return get_embedding_cache_stats().format_summary()


def build_model_cache_key(*, model: str, dimensions: int) -> str:
    """Stable model/version key; dimension changes invalidate prior vectors."""
    return f"{model}@{dimensions}"


def build_text_cache_key(*, model_key: str, text: str) -> str:
    """Hash exact text plus model key so model changes never reuse stale vectors."""
    payload = f"{model_key}\n{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SQLiteEmbeddingCache:
    """Thread-safe SQLite store for embedding vectors keyed by text hash + model."""

    def __init__(self, db_path: Path, model_key: str) -> None:
        self.db_path = db_path
        self.model_key = model_key
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30.0)

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        expected_columns = {"cache_key", "model_key", "dim", "vector"}
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding_cache'"
        ).fetchone()
        if existing is not None:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(embedding_cache)")
            }
            if columns != expected_columns:
                conn.execute("DROP TABLE embedding_cache")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                cache_key TEXT PRIMARY KEY,
                model_key TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector BLOB NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_cache_model "
            "ON embedding_cache(model_key)"
        )

    @staticmethod
    def _pack_vector(vector: list[float]) -> bytes:
        return struct.pack(f"{len(vector)}f", *vector)

    @staticmethod
    def _unpack_vector(blob: bytes, dim: int) -> list[float]:
        return list(struct.unpack(f"{dim}f", blob))

    def get(self, text: str) -> list[float] | None:
        cache_key = build_text_cache_key(model_key=self.model_key, text=text)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT dim, vector FROM embedding_cache "
                "WHERE cache_key = ? AND model_key = ?",
                (cache_key, self.model_key),
            ).fetchone()
        if row is None:
            return None
        dim, blob = row
        return self._unpack_vector(blob, int(dim))

    def put(self, text: str, vector: list[float]) -> None:
        cache_key = build_text_cache_key(model_key=self.model_key, text=text)
        blob = self._pack_vector(vector)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO embedding_cache (cache_key, model_key, dim, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    model_key = excluded.model_key,
                    dim = excluded.dim,
                    vector = excluded.vector
                """,
                (cache_key, self.model_key, len(vector), blob),
            )


@lru_cache
def get_embedding_cache(db_path: str, model_key: str) -> SQLiteEmbeddingCache:
    return SQLiteEmbeddingCache(Path(db_path), model_key)


def resolve_embedding_cache(settings: Settings | None = None) -> SQLiteEmbeddingCache:
    cfg = settings or get_settings()
    model = cfg.embedding_model
    if model == "gemini-embedding-2":
        model = "gemini-embedding-2-preview"
    model_key = build_model_cache_key(model=model, dimensions=cfg.embedding_dimensions)
    return get_embedding_cache(str(cfg.embedding_cache_path.resolve()), model_key)
