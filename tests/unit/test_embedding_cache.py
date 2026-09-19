"""Unit tests for SQLite embedding cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.rag.embedding_cache import (
    SQLiteEmbeddingCache,
    build_model_cache_key,
    build_text_cache_key,
    reset_embedding_cache_stats,
)


def test_cache_key_changes_with_model_or_text() -> None:
    model_a = build_model_cache_key(model="gemini-embedding-2-preview", dimensions=768)
    model_b = build_model_cache_key(model="gemini-embedding-2-preview", dimensions=512)
    text = "duplicate claim submissions must be rejected"
    assert build_text_cache_key(model_key=model_a, text=text) != build_text_cache_key(
        model_key=model_b, text=text
    )
    assert build_text_cache_key(model_key=model_a, text=text) != build_text_cache_key(
        model_key=model_a, text=f"{text}."
    )


def test_sqlite_cache_round_trip(tmp_path: Path) -> None:
    reset_embedding_cache_stats()
    model_key = build_model_cache_key(model="test-model", dimensions=3)
    cache = SQLiteEmbeddingCache(tmp_path / "cache.sqlite", model_key)
    vector = [0.1, 0.2, 0.3]
    cache.put("hello world", vector)
    loaded = cache.get("hello world")
    assert loaded is not None
    assert len(loaded) == len(vector)
    for left, right in zip(loaded, vector, strict=True):
        assert left == pytest.approx(right)
    assert cache.get("other text") is None


def test_model_change_does_not_reuse_old_vectors(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite"
    old_key = build_model_cache_key(model="model-a", dimensions=3)
    new_key = build_model_cache_key(model="model-b", dimensions=3)
    old_cache = SQLiteEmbeddingCache(db_path, old_key)
    new_cache = SQLiteEmbeddingCache(db_path, new_key)
    old_cache.put("same text", [1.0, 2.0, 3.0])
    assert new_cache.get("same text") is None
