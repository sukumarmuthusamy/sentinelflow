"""Retrieval orchestration: hybrid search plus optional reranking."""

from __future__ import annotations

from app.core.settings import Settings, get_settings
from app.rag.models import RerankerMode, RetrievedChunk, VectorStoreBackend
from app.rag.reranker import RerankerPort, build_reranker
from app.rag.vector_store import VectorStorePort, build_vector_store


class RetrievalService:
    """Hybrid retrieval with pluggable vector store and reranker arms."""

    def __init__(
        self,
        *,
        domain_pack_id: str,
        backend: VectorStoreBackend,
        reranker_mode: RerankerMode = RerankerMode.NONE,
        settings: Settings | None = None,
    ) -> None:
        self.domain_pack_id = domain_pack_id
        self.backend = backend
        self.reranker_mode = reranker_mode
        self.settings = settings or get_settings()
        collection_name = f"sentinelflow_{domain_pack_id.replace('-', '_')}"
        self._store: VectorStorePort = build_vector_store(backend, collection_name=collection_name)
        self._reranker: RerankerPort = build_reranker(reranker_mode, self.settings)

    async def initialize(self) -> None:
        await self._store.initialize()

    async def retrieve(self, query: str, *, k: int | None = None) -> list[RetrievedChunk]:
        top_k = k or self.settings.retrieval_top_k
        candidates = await self._store.hybrid_search(
            query,
            k=top_k,
            domain_pack_id=self.domain_pack_id,
        )
        if self.reranker_mode is RerankerMode.NONE:
            return candidates[:top_k]
        return self._reranker.rerank(query, candidates, top_n=top_k)

    async def close(self) -> None:
        await self._store.close()
