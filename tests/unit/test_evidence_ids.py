"""Tests for evidence ID validation."""

from __future__ import annotations

from app.agents.contracts import EvidenceBundle, EvidenceCitation, EvidenceItem
from app.graph.evidence_ids import (
    evidence_id,
    filter_evidence_items,
    validate_bundle_citations,
)
from app.rag.models import ChunkMetadata, DocumentType, RetrievedChunk


def test_filter_rejects_unknown_citation() -> None:
    allowed = {evidence_id("doc-a", "sec-1")}
    items = [
        EvidenceItem(
            hypothesis_id="h1",
            stance="support",
            citation=EvidenceCitation(
                document_id="doc-a",
                section_id="sec-1",
                document_type="requirement",
                relevance_score=0.9,
            ),
        ),
        EvidenceItem(
            hypothesis_id="h2",
            stance="support",
            citation=EvidenceCitation(
                document_id="doc-b",
                section_id="sec-9",
                document_type="requirement",
                relevance_score=0.9,
            ),
        ),
    ]
    kept, rejected = filter_evidence_items(items, allowed)
    assert len(kept) == 1
    assert rejected == [evidence_id("doc-b", "sec-9")]


def test_validate_bundle_citations() -> None:
    allowed = {evidence_id("doc-a", "sec-1")}
    bundle = EvidenceBundle(
        items=[
            EvidenceItem(
                hypothesis_id="h1",
                stance="support",
                citation=EvidenceCitation(
                    document_id="doc-a",
                    section_id="sec-1",
                    document_type="adr",
                    relevance_score=1.0,
                ),
            ),
        ],
    )
    validated, rejected = validate_bundle_citations(bundle, allowed)
    assert not rejected
    assert len(validated.items) == 1


def test_collect_from_retrieved_chunk() -> None:
    from app.graph.evidence_ids import collect_retrieved_evidence_ids

    chunk = RetrievedChunk(
        content="text",
        metadata=ChunkMetadata(
            document_id="doc-x",
            section_id="s1",
            document_type=DocumentType.ADR,
            version="1",
            effective_date="2026-01-01",
            source_path="knowledge/x.md",
            domain_pack_id="healthcare-claims",
        ),
    )
    assert collect_retrieved_evidence_ids([chunk]) == {evidence_id("doc-x", "s1")}
