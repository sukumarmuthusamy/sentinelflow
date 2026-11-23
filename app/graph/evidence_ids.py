"""Evidence ID helpers and post-generation citation validation (plan section 11)."""

from __future__ import annotations

from app.agents.contracts import (
    AssertionAssessment,
    EvidenceBundle,
    EvidenceCitation,
    EvidenceItem,
    SecurityFinding,
    VerificationReport,
)
from app.rag.models import ChunkMetadata, RetrievedChunk


def evidence_id(document_id: str, section_id: str) -> str:
    """Stable evidence identifier for citations and retrieval chunks."""
    return f"{document_id}::{section_id}"


def evidence_id_from_citation(citation: EvidenceCitation) -> str:
    return evidence_id(citation.document_id, citation.section_id)


def evidence_id_from_metadata(metadata: ChunkMetadata) -> str:
    return evidence_id(metadata.document_id, metadata.section_id)


def collect_retrieved_evidence_ids(chunks: list[RetrievedChunk]) -> set[str]:
    return {evidence_id_from_metadata(chunk.metadata) for chunk in chunks}


def filter_evidence_items(
    items: list[EvidenceItem],
    allowed_ids: set[str],
) -> tuple[list[EvidenceItem], list[str]]:
    """Drop items whose citations are absent from the retrieved set."""
    kept: list[EvidenceItem] = []
    rejected: list[str] = []
    for item in items:
        cited = evidence_id_from_citation(item.citation)
        if cited in allowed_ids:
            kept.append(item)
        else:
            rejected.append(cited)
    return kept, rejected


def filter_security_findings(
    findings: list[SecurityFinding],
    allowed_ids: set[str],
) -> tuple[list[SecurityFinding], list[str]]:
    """Strip citations absent from the retrieved set, but keep every finding.

    A finding with only invalid citations stays, with no evidence: it then counts as
    an unsupported (possibly critical) assertion instead of silently vanishing.
    """
    kept: list[SecurityFinding] = []
    rejected: list[str] = []
    for finding in findings:
        valid_ids = [eid for eid in finding.evidence_ids if eid in allowed_ids]
        rejected.extend(eid for eid in finding.evidence_ids if eid not in allowed_ids)
        kept.append(finding.model_copy(update={"evidence_ids": valid_ids}))
    return kept, rejected


def filter_verification_report(
    report: VerificationReport,
    allowed_ids: set[str],
) -> tuple[VerificationReport, list[str]]:
    rejected: list[str] = []
    assessments: list[AssertionAssessment] = []
    for assessment in report.assertion_assessments:
        valid_ids = [eid for eid in assessment.evidence_ids if eid in allowed_ids]
        invalid = [eid for eid in assessment.evidence_ids if eid not in allowed_ids]
        rejected.extend(invalid)
        assessments.append(
            assessment.model_copy(update={"evidence_ids": valid_ids}),
        )
    return report.model_copy(update={"assertion_assessments": assessments}), rejected


def validate_bundle_citations(
    bundle: EvidenceBundle,
    allowed_ids: set[str],
) -> tuple[EvidenceBundle, list[str]]:
    items, rejected = filter_evidence_items(bundle.items, allowed_ids)
    return bundle.model_copy(update={"items": items}), rejected
