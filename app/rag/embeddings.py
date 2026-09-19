"""Gemini embedding pipeline for retrieval."""

from __future__ import annotations

from typing import cast

from langchain_core.embeddings import Embeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pydantic import SecretStr

from app.core.settings import Settings, get_settings
from app.rag.embedding_cache import (
    SQLiteEmbeddingCache,
    get_embedding_cache_stats,
    resolve_embedding_cache,
)


def _resolve_model_name(settings: Settings) -> str:
    model = settings.embedding_model
    if model == "gemini-embedding-2":
        return "gemini-embedding-2-preview"
    return model


def _build_inner_embeddings(settings: Settings) -> GoogleGenerativeAIEmbeddings:
    api_key = settings.require_gemini_api_key()
    return GoogleGenerativeAIEmbeddings(
        model=_resolve_model_name(settings),
        google_api_key=SecretStr(api_key),
        output_dimensionality=settings.embedding_dimensions,
    )


class CachedEmbeddings(Embeddings):
    """Embeddings wrapper that checks SQLite cache before calling Gemini."""

    def __init__(self, inner: Embeddings, cache: SQLiteEmbeddingCache) -> None:
        self._inner = inner
        self._cache = cache

    def _resolve(self, texts: list[str]) -> list[list[float] | None]:
        stats = get_embedding_cache_stats()
        resolved: list[list[float] | None] = []
        for text in texts:
            cached = self._cache.get(text)
            if cached is not None:
                stats.record_hit()
                resolved.append(cached)
            else:
                resolved.append(None)
        return resolved

    def _store_misses(self, texts: list[str], vectors: list[list[float]]) -> None:
        stats = get_embedding_cache_stats()
        for text, vector in zip(texts, vectors, strict=True):
            self._cache.put(text, vector)
            stats.record_miss()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resolved = self._resolve(texts)
        miss_indices = [index for index, vector in enumerate(resolved) if vector is None]
        if miss_indices:
            miss_texts = [texts[index] for index in miss_indices]
            fetched = self._inner.embed_documents(miss_texts)
            self._store_misses(miss_texts, fetched)
            for index, vector in zip(miss_indices, fetched, strict=True):
                resolved[index] = vector
        return cast(list[list[float]], resolved)

    def embed_query(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            get_embedding_cache_stats().record_hit()
            return cached
        vector = self._inner.embed_query(text)
        self._cache.put(text, vector)
        get_embedding_cache_stats().record_miss()
        return vector

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resolved = self._resolve(texts)
        miss_indices = [index for index, vector in enumerate(resolved) if vector is None]
        if miss_indices:
            miss_texts = [texts[index] for index in miss_indices]
            fetched = await self._inner.aembed_documents(miss_texts)
            self._store_misses(miss_texts, fetched)
            for index, vector in zip(miss_indices, fetched, strict=True):
                resolved[index] = vector
        return cast(list[list[float]], resolved)

    async def aembed_query(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            get_embedding_cache_stats().record_hit()
            return cached
        vector = await self._inner.aembed_query(text)
        self._cache.put(text, vector)
        get_embedding_cache_stats().record_miss()
        return vector


def build_embeddings(settings: Settings | None = None) -> Embeddings:
    """Create Gemini embedding client with local SQLite caching."""
    cfg = settings or get_settings()
    cache = resolve_embedding_cache(cfg)
    return CachedEmbeddings(inner=_build_inner_embeddings(cfg), cache=cache)
