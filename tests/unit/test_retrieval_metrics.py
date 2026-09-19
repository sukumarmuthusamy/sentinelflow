"""Unit tests for retrieval metrics (no external services)."""

from app.rag.models import ChunkMetadata, DocumentType, RequiredEvidence, RetrievedChunk
from evals.retrieval.metrics import aggregate_metrics, ndcg_at_k, recall_at_k, reciprocal_rank


def _chunk(document_id: str, section_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        content="body",
        metadata=ChunkMetadata(
            document_id=document_id,
            section_id=section_id,
            document_type=DocumentType.REQUIREMENT,
            service="svc-core-api",
            version="1.0.0",
            effective_date="2025-01-01",
            source_path=f"knowledge/{document_id}.md",
            domain_pack_id="healthcare-claims",
        ),
        score=0.5,
    )


def test_recall_and_mrr_for_matching_section() -> None:
    retrieved = [
        _chunk("requirements/a", "other"),
        _chunk("requirements/b", "target-section"),
    ]
    required = [RequiredEvidence(document_id="requirements/b", section_id="target-section")]
    assert recall_at_k(retrieved, required, k=2) == 1.0
    assert reciprocal_rank(retrieved, required) == 0.5


def test_ndcg_zero_when_no_match() -> None:
    retrieved = [_chunk("requirements/a", "x")]
    required = [RequiredEvidence(document_id="requirements/z", section_id="y")]
    assert ndcg_at_k(retrieved, required, k=10) == 0.0


def test_aggregate_metrics() -> None:
    per_query = [
        ([_chunk("requirements/a", "s")], [RequiredEvidence(document_id="requirements/a")]),
        ([_chunk("requirements/b", "s")], [RequiredEvidence(document_id="requirements/z")]),
    ]
    metrics = aggregate_metrics(
        arm="pgvector+rerank:none",
        domain_pack_id="healthcare-claims",
        per_query=per_query,
    )
    assert metrics.query_count == 2
    assert metrics.recall_at_10 == 0.5
    assert metrics.mrr == 0.5
