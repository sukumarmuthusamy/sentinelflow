"""Retrieval evaluation metrics: recall@k, MRR, and nDCG."""

from __future__ import annotations

import math

from app.rag.models import RequiredEvidence, RetrievalMetrics, RetrievedChunk


def _matches(required: RequiredEvidence, chunk: RetrievedChunk) -> bool:
    if chunk.metadata.document_id != required.document_id:
        return False
    if required.section_id is None:
        return True
    return chunk.metadata.section_id == required.section_id


def recall_at_k(
    retrieved: list[RetrievedChunk],
    required: list[RequiredEvidence],
    *,
    k: int,
) -> float:
    if not required:
        return 1.0
    top = retrieved[:k]
    for item in required:
        if any(_matches(item, chunk) for chunk in top):
            return 1.0
    return 0.0


def reciprocal_rank(retrieved: list[RetrievedChunk], required: list[RequiredEvidence]) -> float:
    for rank, chunk in enumerate(retrieved, start=1):
        if any(_matches(item, chunk) for item in required):
            return 1.0 / rank
    return 0.0


def ndcg_at_k(
    retrieved: list[RetrievedChunk],
    required: list[RequiredEvidence],
    *,
    k: int,
) -> float:
    if not required:
        return 1.0
    dcg = 0.0
    for rank, chunk in enumerate(retrieved[:k], start=1):
        rel = 1.0 if any(_matches(item, chunk) for item in required) else 0.0
        if rel:
            dcg += rel / math.log2(rank + 1)
    ideal_hits = min(len(required), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0:
        return 0.0
    return dcg / idcg


def aggregate_metrics(
    *,
    arm: str,
    domain_pack_id: str,
    per_query: list[tuple[list[RetrievedChunk], list[RequiredEvidence]]],
    query_batch: str = "all",
) -> RetrievalMetrics:
    if not per_query:
        return RetrievalMetrics(
            arm=arm,
            domain_pack_id=domain_pack_id,
            query_batch="all",
            query_count=0,
            recall_at_5=0.0,
            recall_at_10=0.0,
            mrr=0.0,
            ndcg_at_10=0.0,
        )
    recall5 = sum(recall_at_k(r, req, k=5) for r, req in per_query) / len(per_query)
    recall10 = sum(recall_at_k(r, req, k=10) for r, req in per_query) / len(per_query)
    mrr = sum(reciprocal_rank(r, req) for r, req in per_query) / len(per_query)
    ndcg10 = sum(ndcg_at_k(r, req, k=10) for r, req in per_query) / len(per_query)
    return RetrievalMetrics(
        arm=arm,
        domain_pack_id=domain_pack_id,
        query_batch=query_batch,
        query_count=len(per_query),
        recall_at_5=round(recall5, 4),
        recall_at_10=round(recall10, 4),
        mrr=round(mrr, 4),
        ndcg_at_10=round(ndcg10, 4),
    )
