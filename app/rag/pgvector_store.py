"""pgvector adapter using PGVectorStore with hybrid tsvector + RRF."""

from __future__ import annotations

from langchain_postgres import PGEngine, PGVectorStore
from langchain_postgres.v2.engine import Column
from langchain_postgres.v2.hybrid_search_config import HybridSearchConfig, reciprocal_rank_fusion

from app.core.settings import get_settings
from app.rag.embeddings import build_embeddings
from app.rag.models import KnowledgeChunk, RetrievedChunk, VectorStoreBackend
from app.rag.vector_store import BaseVectorStoreAdapter

TSV_COLUMN = "content_tsv"
METADATA_COLUMNS = [
    "document_id",
    "section_id",
    "document_type",
    "service",
    "version",
    "effective_date",
    "source_path",
    "domain_pack_id",
]


def _async_database_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _build_hybrid_config(collection_name: str, *, fetch_top_k: int) -> HybridSearchConfig:
    return HybridSearchConfig(
        tsv_column=TSV_COLUMN,
        fusion_function=reciprocal_rank_fusion,
        fusion_function_parameters={"fetch_top_k": fetch_top_k},
        primary_top_k=max(fetch_top_k, 20),
        secondary_top_k=max(fetch_top_k, 20),
        index_name=f"{collection_name}_tsv_gin",
        index_type="GIN",
    )


class PGVectorStoreAdapter(BaseVectorStoreAdapter):
    """Default vector store backed by pgvector + tsvector hybrid search."""

    backend = VectorStoreBackend.PGVECTOR

    def __init__(self, *, collection_name: str) -> None:
        self.collection_name = collection_name
        self.settings = get_settings()
        self._engine: PGEngine | None = None
        self._store: PGVectorStore | None = None
        self._hybrid_config: HybridSearchConfig | None = None

    async def initialize(self) -> None:
        if self._store is not None:
            return

        embeddings = build_embeddings(self.settings)
        db_url = _async_database_url(self.settings.database_url)
        self._engine = PGEngine.from_connection_string(url=db_url)
        self._hybrid_config = _build_hybrid_config(
            self.collection_name,
            fetch_top_k=max(self.settings.retrieval_top_k, 20),
        )
        assert self._engine is not None and self._hybrid_config is not None

        self._store = PGVectorStore.create_sync(
            self._engine,
            embeddings,
            self.collection_name,
            metadata_columns=METADATA_COLUMNS,
            hybrid_search_config=self._hybrid_config,
            k=self.settings.retrieval_top_k,
        )

    async def reset(self) -> None:
        embeddings = build_embeddings(self.settings)
        if self._engine is None:
            self._engine = PGEngine.from_connection_string(
                url=_async_database_url(self.settings.database_url)
            )
        self._hybrid_config = _build_hybrid_config(
            self.collection_name,
            fetch_top_k=max(self.settings.retrieval_top_k, 20),
        )
        assert self._engine is not None and self._hybrid_config is not None

        self._engine.init_vectorstore_table(
            table_name=self.collection_name,
            vector_size=self.settings.embedding_dimensions,
            metadata_columns=[Column(name=name, data_type="TEXT") for name in METADATA_COLUMNS],
            hybrid_search_config=self._hybrid_config,
            overwrite_existing=True,
        )
        self._store = PGVectorStore.create_sync(
            self._engine,
            embeddings,
            self.collection_name,
            metadata_columns=METADATA_COLUMNS,
            hybrid_search_config=self._hybrid_config,
            k=self.settings.retrieval_top_k,
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
        filter_payload = self.domain_filter(domain_pack_id)
        results = await self._store.asimilarity_search_with_score(
            query,
            k=k,
            filter=filter_payload,
        )
        return [
            self.document_to_retrieved(document, score=score, rank=index + 1)
            for index, (document, score) in enumerate(results)
        ]

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.close()
