"""Vector store port implemented by pgvector and Qdrant adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from langchain_core.documents import Document

from app.rag.models import KnowledgeChunk, RetrievedChunk, VectorStoreBackend


@runtime_checkable
class VectorStorePort(Protocol):
    """Hybrid retrieval interface shared by pgvector and Qdrant backends."""

    backend: VectorStoreBackend

    async def initialize(self) -> None:
        """Provision schema/collection and indexes."""

    async def reset(self) -> None:
        """Drop and recreate storage for the domain pack collection."""

    async def add_chunks(self, chunks: list[KnowledgeChunk]) -> int:
        """Embed and index chunks; return count indexed."""

    async def hybrid_search(
        self,
        query: str,
        *,
        k: int = 10,
        domain_pack_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """Run hybrid dense+lexical retrieval."""

    async def close(self) -> None:
        """Release connections."""


class BaseVectorStoreAdapter(ABC):
    """Shared helpers for vector store adapters."""

    backend: VectorStoreBackend

    @staticmethod
    def chunks_to_documents(chunks: list[KnowledgeChunk]) -> list[Document]:
        return [
            Document(page_content=chunk.content, metadata=chunk.metadata.to_langchain_metadata())
            for chunk in chunks
        ]

    @staticmethod
    def document_to_retrieved(doc: Document, score: float, rank: int) -> RetrievedChunk:
        metadata = doc.metadata
        from app.rag.models import ChunkMetadata, DocumentType

        return RetrievedChunk(
            content=doc.page_content,
            metadata=ChunkMetadata(
                document_id=str(metadata["document_id"]),
                section_id=str(metadata["section_id"]),
                document_type=DocumentType(str(metadata["document_type"])),
                service=str(metadata.get("service") or "") or None,
                version=str(metadata["version"]),
                effective_date=str(metadata["effective_date"]),
                source_path=str(metadata["source_path"]),
                domain_pack_id=str(metadata["domain_pack_id"]),
            ),
            score=score,
            rank=rank,
        )

    @staticmethod
    def domain_filter(domain_pack_id: str | None) -> dict[str, str] | None:
        if domain_pack_id is None:
            return None
        return {"domain_pack_id": domain_pack_id}

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def reset(self) -> None: ...

    @abstractmethod
    async def add_chunks(self, chunks: list[KnowledgeChunk]) -> int: ...

    @abstractmethod
    async def hybrid_search(
        self,
        query: str,
        *,
        k: int = 10,
        domain_pack_id: str | None = None,
    ) -> list[RetrievedChunk]: ...

    async def close(self) -> None:
        return None


def build_vector_store(
    backend: VectorStoreBackend,
    *,
    collection_name: str,
) -> VectorStorePort:
    """Factory for the configured vector store adapter."""
    if backend is VectorStoreBackend.PGVECTOR:
        from app.rag.pgvector_store import PGVectorStoreAdapter

        return PGVectorStoreAdapter(collection_name=collection_name)
    from app.rag.qdrant_store import QdrantStoreAdapter

    return QdrantStoreAdapter(collection_name=collection_name)
