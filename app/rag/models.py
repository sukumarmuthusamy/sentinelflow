"""Typed models for RAG ingestion, retrieval, and evaluation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class DocumentType(StrEnum):
    """Knowledge document categories aligned with the domain pack corpus layout."""

    REQUIREMENT = "requirement"
    CONTRACT = "contract"
    ADR = "adr"
    TEST_PLAN = "test_plan"
    RELEASE_POLICY = "release_policy"
    RUNBOOK = "runbook"
    INCIDENT = "incident"
    OWNERSHIP = "ownership"


class ChunkMetadata(BaseModel):
    """Metadata attached to every indexed chunk per plan section 6."""

    document_id: str
    section_id: str
    document_type: DocumentType
    service: str | None = None
    version: str
    effective_date: str
    source_path: str
    domain_pack_id: str

    def to_langchain_metadata(self) -> dict[str, Any]:
        """Serialize for vector store metadata columns."""
        return {
            "document_id": self.document_id,
            "section_id": self.section_id,
            "document_type": self.document_type.value,
            "service": self.service or "",
            "version": self.version,
            "effective_date": self.effective_date,
            "source_path": self.source_path,
            "domain_pack_id": self.domain_pack_id,
        }


class KnowledgeChunk(BaseModel):
    """A structure-aware chunk ready for embedding and indexing."""

    content: str
    metadata: ChunkMetadata


class RetrievedChunk(BaseModel):
    """A chunk returned from hybrid search, optionally reranked."""

    content: str
    metadata: ChunkMetadata
    score: float = 0.0
    rank: int | None = None


class RequiredEvidence(BaseModel):
    """Gold-label pointer to a specific document section."""

    document_id: str
    section_id: str | None = None


class QueryBatch(StrEnum):
    """Eval query difficulty batch for separate reporting."""

    EASY = "easy"
    HARD = "hard"


class RetrievalQueryLabel(BaseModel):
    """Labeled query for retrieval-only evaluation."""

    query_id: str
    domain_pack_id: str
    query: str
    required_evidence: list[RequiredEvidence]
    query_batch: QueryBatch = QueryBatch.EASY
    notes: str | None = None


class RetrievalMetrics(BaseModel):
    """Aggregate retrieval metrics for one configuration arm."""

    arm: str
    domain_pack_id: str
    query_batch: str
    query_count: int
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float


class RerankerMode(StrEnum):
    """Reranker ablation arms."""

    NONE = "none"
    LOCAL = "local"
    VOYAGE = "voyage"


class VectorStoreBackend(StrEnum):
    """Supported vector store implementations behind the port."""

    PGVECTOR = "pgvector"
    QDRANT = "qdrant"


class IngestReport(BaseModel):
    """Summary of a corpus ingestion run."""

    domain_pack_id: str
    source_documents: int
    chunks_indexed: int
    backend: VectorStoreBackend
