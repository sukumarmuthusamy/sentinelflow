"""Retrieval-only evaluation harness."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.rag.models import (
    QueryBatch,
    RequiredEvidence,
    RerankerMode,
    RetrievalMetrics,
    RetrievalQueryLabel,
    VectorStoreBackend,
)
from app.rag.retrieval import RetrievalService
from evals.retrieval.metrics import aggregate_metrics

# Stay under Gemini free-tier embed RPM (~100/min) during eval sweeps.
_QUERY_PACE_SECONDS = 0.7
# Voyage rerank without billing: 3 RPM and 10K TPM — pace for TPM (~5k tokens/call at k=10).
_VOYAGE_RERANK_PACE_SECONDS = 32.0


def label_paths(domain_pack_id: str) -> list[Path]:
    root = Path("evals/retrieval/labels")
    return [
        root / f"{domain_pack_id}-easy.jsonl",
        root / f"{domain_pack_id}-hard.jsonl",
    ]


def load_labels(
    domain_pack_id: str,
    *,
    query_batch: QueryBatch | None = None,
) -> list[RetrievalQueryLabel]:
    labels: list[RetrievalQueryLabel] = []
    for path in label_paths(domain_pack_id):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            labels.append(RetrievalQueryLabel.model_validate(payload))
    if query_batch is not None:
        labels = [label for label in labels if label.query_batch is query_batch]
    return labels


async def evaluate_arm(
    *,
    domain_pack_id: str,
    backend: VectorStoreBackend,
    reranker_mode: RerankerMode,
    k: int = 10,
    query_batch: QueryBatch | None = None,
) -> RetrievalMetrics:
    labels = load_labels(domain_pack_id, query_batch=query_batch)
    batch_label = query_batch.value if query_batch is not None else "all"
    service = RetrievalService(
        domain_pack_id=domain_pack_id,
        backend=backend,
        reranker_mode=reranker_mode,
    )
    query_pace_seconds = (
        _VOYAGE_RERANK_PACE_SECONDS
        if reranker_mode is RerankerMode.VOYAGE
        else _QUERY_PACE_SECONDS
    )
    per_query: list[tuple[list, list[RequiredEvidence]]] = []
    try:
        await service.initialize()
        for index, label in enumerate(labels):
            if index > 0:
                await asyncio.sleep(query_pace_seconds)
            retrieved = await service.retrieve(label.query, k=k)
            per_query.append((retrieved, label.required_evidence))
    finally:
        await service.close()

    arm = f"{backend.value}+rerank:{reranker_mode.value}"
    return aggregate_metrics(
        arm=arm,
        domain_pack_id=domain_pack_id,
        per_query=per_query,
        query_batch=batch_label,
    )
