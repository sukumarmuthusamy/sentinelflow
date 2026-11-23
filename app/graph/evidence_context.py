"""Retrieved evidence as graph state, and the bounded view the Evidence Verifier gets.

Chunks from every retrieving node (Evidence Investigator, Security Auditor, repair)
merge into one deduplicated list keyed by evidence ID. The verifier is shown the
actual chunk text, capped per chunk and in total, and may cite only chunks it was
shown: an ID that was retrieved but withheld by the cap is not citable.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from app.graph.evidence_ids import evidence_id_from_metadata
from app.rag.models import RetrievedChunk

ChunkSource = Literal["evidence_investigator", "security_auditor", "repair"]

MAX_VERIFIER_CHUNKS = 40
MAX_CHUNK_CHARS = 1_500
MAX_VERIFIER_EVIDENCE_CHARS = 40_000


class EvidenceChunk(BaseModel):
    """One retrieved chunk as stored in graph state (checkpoint-safe JSON)."""

    evidence_id: str
    document_id: str
    section_id: str
    document_type: str
    service: str | None = None
    content: str
    source: ChunkSource


def chunk_records(chunks: list[RetrievedChunk], *, source: ChunkSource) -> list[dict[str, Any]]:
    return [
        EvidenceChunk(
            evidence_id=evidence_id_from_metadata(chunk.metadata),
            document_id=chunk.metadata.document_id,
            section_id=chunk.metadata.section_id,
            document_type=str(chunk.metadata.document_type),
            service=chunk.metadata.service,
            content=chunk.content,
            source=source,
        ).model_dump(mode="json")
        for chunk in chunks
    ]


def merge_unique_chunks(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """LangGraph reducer: union by evidence ID, first writer kept, stable order.

    Parallel branches write in the same superstep, so the result is sorted by
    evidence ID to be independent of branch completion order.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for record in [*(existing or []), *(incoming or [])]:
        by_id.setdefault(str(record["evidence_id"]), record)
    return [by_id[key] for key in sorted(by_id)]


class VerifierEvidence(BaseModel):
    """What the verifier is shown, and what was held back by the size caps."""

    shown: list[EvidenceChunk]
    withheld_ids: list[str]

    @property
    def shown_ids(self) -> set[str]:
        return {chunk.evidence_id for chunk in self.shown}

    def content_by_id(self) -> dict[str, str]:
        return {chunk.evidence_id: chunk.content for chunk in self.shown}


def build_verifier_evidence(
    records: list[dict[str, Any]],
    *,
    priority_ids: set[str],
    max_chunks: int = MAX_VERIFIER_CHUNKS,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
    max_total_chars: int = MAX_VERIFIER_EVIDENCE_CHARS,
) -> VerifierEvidence:
    """Bound the evidence the verifier sees; specialist-cited chunks go first."""
    chunks = [EvidenceChunk.model_validate(record) for record in records]
    ordered = sorted(
        chunks, key=lambda chunk: (chunk.evidence_id not in priority_ids, chunk.evidence_id)
    )
    shown: list[EvidenceChunk] = []
    withheld: list[str] = []
    total = 0
    for chunk in ordered:
        content = chunk.content[:max_chunk_chars]
        if len(shown) >= max_chunks or total + len(content) > max_total_chars:
            withheld.append(chunk.evidence_id)
            continue
        shown.append(chunk.model_copy(update={"content": content}))
        total += len(content)
    return VerifierEvidence(shown=shown, withheld_ids=withheld)
