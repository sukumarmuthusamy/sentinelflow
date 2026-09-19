"""Qdrant adapter with server-side hybrid BM25 + dense fusion."""

from __future__ import annotations

from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from qdrant_client.http import models as qdrant_models

from app.core.settings import get_settings
from app.rag.embeddings import build_embeddings
from app.rag.models import KnowledgeChunk, RetrievedChunk, VectorStoreBackend
from app.rag.vector_store import BaseVectorStoreAdapter


class QdrantStoreAdapter(BaseVectorStoreAdapter):
    """Alternative vector store using Qdrant hybrid retrieval."""

    backend = VectorStoreBackend.QDRANT

    def __init__(self, *, collection_name: str) -> None:
        self.collection_name = collection_name
        self.settings = get_settings()
        self._store: QdrantVectorStore | None = None

    def _client_options(self) -> dict[str, str]:
        options: dict[str, str] = {"url": self.settings.qdrant_url}
        if self.settings.qdrant_api_key is not None:
            key = self.settings.qdrant_api_key.get_secret_value().strip()
            if key:
                options["api_key"] = key
        return options

    async def initialize(self) -> None:
        if self._store is not None:
            return
        embeddings = build_embeddings(self.settings)
        sparse = FastEmbedSparse(model_name="Qdrant/bm25")
        self._store = QdrantVectorStore.construct_instance(
            embedding=embeddings,
            sparse_embedding=sparse,
            retrieval_mode=RetrievalMode.HYBRID,
            collection_name=self.collection_name,
            client_options=self._client_options(),
            vector_name="dense",
            sparse_vector_name="sparse",
            force_recreate=False,
        )

    async def reset(self) -> None:
        embeddings = build_embeddings(self.settings)
        sparse = FastEmbedSparse(model_name="Qdrant/bm25")
        self._store = QdrantVectorStore.construct_instance(
            embedding=embeddings,
            sparse_embedding=sparse,
            retrieval_mode=RetrievalMode.HYBRID,
            collection_name=self.collection_name,
            client_options=self._client_options(),
            vector_name="dense",
            sparse_vector_name="sparse",
            force_recreate=True,
        )

    async def add_chunks(self, chunks: list[KnowledgeChunk]) -> int:
        await self.initialize()
        assert self._store is not None
        documents = self.chunks_to_documents(chunks)
        if not documents:
            return 0
        await self._store.aadd_documents(documents)
        return len(documents)

    async def hybrid_search(
        self,
        query: str,
        *,
        k: int = 10,
        domain_pack_id: str | None = None,
    ) -> list[RetrievedChunk]:
        await self.initialize()
        assert self._store is not None
        qdrant_filter = None
        if domain_pack_id is not None:
            qdrant_filter = qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="metadata.domain_pack_id",
                        match=qdrant_models.MatchValue(value=domain_pack_id),
                    )
                ]
            )
        results = await self._store.asimilarity_search_with_score(
            query,
            k=k,
            filter=qdrant_filter,
        )
        return [
            self.document_to_retrieved(document, score=score, rank=index + 1)
            for index, (document, score) in enumerate(results)
        ]

    async def close(self) -> None:
        if self._store is not None and self._store.client is not None:
            self._store.client.close()
